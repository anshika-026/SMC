"""
footfall.py

Unique footfall across the SMC camera feeds, using the body-appearance
Re-ID engine vendored in app/reid/ (see docs/UNIQUE_FOOTFALL.md for how the
engine works and its real-world limits). Ported from MAINDV's footfall.py.

The counted cameras are exactly the ones configured in backend/.env
(CAMERA_1_*, CAMERA_2_*), not a database table. Both share ONE identity
gallery: a person seen on camera 1 and later on camera 2 matches the same
PERSON_NNN and is counted once. The gallery is rebuilt the moment either
camera enrolls someone.

Each camera has an RTSP reader thread (camera_reader.py) holding the latest
frame, and a sampling thread that hands that frame to Re-ID every
REID_MOT_INTERVAL_SECONDS, whether or not anyone has the dashboard open.
"""

import concurrent.futures
import logging
import threading
import time
from collections import Counter
from pathlib import Path

import numpy as np

# Imported here, i.e. on the main thread while the app starts, not lazily
# inside a camera thread: face detection (ultralytics) and Re-ID (torchreid)
# each import torchvision on first use, and two camera threads doing that at
# the same moment fail with "cannot import name ... from partially
# initialized module 'torchvision.transforms'" — which disabled both face
# recognition and footfall on a live restart.
import torchvision.transforms  # noqa: E402,F401
from torchreid.reid.utils import FeatureExtractor  # noqa: E402,F401

from . import camera_reader, config, lifecycle, resilience, storage
from .reid import config as reid_config
from .reid import peopleid_gallery, reid_db, reid_worker

log = logging.getLogger("footfall")

# A track's pending_new_person samples are checked against the shared
# gallery once more before a new identity is created. If at least this
# fraction of them already match one existing person, it's that person —
# typically someone another gate enrolled in the seconds since this track's
# samples were collected.
ENROLL_RECHECK_MATCH_FRACTION = 0.5


class _SharedGallery:
    """The single identity gallery every gate matches against. reload()
    builds a complete new VectorGallery and swaps the reference in one
    assignment, so a gate thread mid-search never sees a half-rebuilt
    matrix (VectorGallery.reload itself updates two arrays separately)."""

    def __init__(self):
        self._gallery = self._build()

    @staticmethod
    def _build() -> peopleid_gallery.VectorGallery:
        return peopleid_gallery.VectorGallery(
            embedding_loader=reid_db.load_all_embeddings,
            similarity_threshold=reid_config.REID_SIMILARITY_THRESHOLD,
            min_margin=reid_config.REID_MIN_MARGIN,
            duplicate_identities_expected=True,  # auto-enrolled — see VectorGallery.__init__
        )

    def reload(self) -> None:
        self._gallery = self._build()

    def best_match(self, embedding, similarity_threshold=None):
        return self._gallery.best_match(embedding, similarity_threshold=similarity_threshold)

    @property
    def person_count(self) -> int:
        return self._gallery.person_count


class _GateRunner:
    """Per-gate Re-ID state plus a single-worker executor, so a slow
    detection pass delays only this gate's counting — never the video
    broadcast, and never another gate."""

    def __init__(self, camera_id: int, gallery: _SharedGallery, generation: int = 0):
        self.camera_id = camera_id
        # Which reset() era this runner belongs to — a frame already being
        # processed when a reset happens must not write into the fresh count.
        self.generation = generation
        self.state = reid_worker.ReidWorkerState(camera_config=reid_db.get_camera_config(camera_id))
        self.state.gallery = gallery  # replace the private copy with the shared one
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"footfall-{camera_id}")
        self.future: concurrent.futures.Future | None = None
        self.last_submit = 0.0


class FootfallService:
    def __init__(self):
        self._gallery: _SharedGallery | None = None
        self._runners: dict[int, _GateRunner] = {}
        # Per-gate failure tracking that outlives a rebuilt runner (see _process).
        self._health: dict[int, resilience.FeatureHealth] = {}
        self._readers: dict[int, camera_reader.CameraReader] = {}
        self._lock = threading.Lock()
        # Serializes every reid_db write + gallery rebuild across gates, so
        # two gates can't both enroll the same newly-arrived person.
        self._write_lock = threading.Lock()
        # (camera_id, track_id) -> (person_id, last_seen): one sighting
        # event per person per track, not one per processed frame.
        self._sighted: dict[tuple[int, int], tuple[int, float]] = {}
        self._generation = 0
        self._started = False

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
        reid_db.init_db()
        self._gallery = _SharedGallery()
        if not reid_config.REID_MODEL_PATH:
            log.warning(
                "footfall: Re-ID model osnet_x0_25_msmt17.pth not found in backend/models — using the "
                "ImageNet fallback, which over-counts badly. Run `python -m scripts.fetch_reid_model` from backend/."
            )
        self._ensure_detector_weights()
        for cam in config.cameras():
            reader = camera_reader.CameraReader(cam["id"], cam["name"], cam["url"])
            self._readers[cam["id"]] = reader
            reader.start()
            threading.Thread(target=self._sample_loop, args=(reader,), daemon=True,
                             name=f"footfall-sample-{cam['id']}").start()
            log.info("footfall: counting on %s (%s)", cam["name"], config.redact(cam["url"]))
        if not self._readers:
            log.warning("footfall: no cameras configured — set CAMERA_1_URL / CAMERA_2_URL in backend/.env")

    @staticmethod
    def _ensure_detector_weights() -> None:
        """Fetches the YOLOv8 weights once, before any camera starts. Left to
        ultralytics' lazy download, both cameras fetch it at the same moment
        and one rename fails with "Access is denied" on Windows."""
        path = Path(reid_config.PEOPLEID_MOT_MODEL_PATH)
        if path.exists() or config.MODEL_OFFLINE_MODE:
            return
        try:
            from ultralytics.utils.downloads import attempt_download_asset

            path.parent.mkdir(parents=True, exist_ok=True)
            attempt_download_asset(path)
        except Exception:
            log.exception("footfall: could not download %s; the tracker will retry on first use", path.name)

    def _sample_loop(self, reader: camera_reader.CameraReader) -> None:
        """Feeds this camera's newest frame to Re-ID at the engine's cadence.
        A frame older than a few intervals (stream stalled) is not re-used."""
        interval = reid_config.REID_MOT_INTERVAL_SECONDS
        last_ts = 0.0
        while not lifecycle.wait(interval):
            frame, ts = reader.latest()
            if frame is None or ts == last_ts or time.time() - ts > interval * 5:
                continue
            if not self.health(reader.camera_id).allow():
                continue
            last_ts = ts
            self.deliver(reader.camera_id, frame, ts)

    def health(self, camera_id: int) -> resilience.FeatureHealth:
        h = self._health.get(camera_id)
        if h is None:
            h = self._health.setdefault(camera_id, resilience.health_for("footfall", camera_id))
        return h

    def deliver(self, camera_id: int, frame: np.ndarray, ts: float) -> None:
        runner = self._runners.get(camera_id)
        if runner is None:
            with self._lock:
                runner = self._runners.setdefault(camera_id, _GateRunner(camera_id, self._gallery, self._generation))
        if runner.future is not None and not runner.future.done():
            return  # previous look still being processed: skip this one
        runner.future = runner.executor.submit(self._process, runner, frame, None, ts)

    def frame_jpeg(self, camera_id: int) -> bytes | None:
        reader = self._readers.get(camera_id)
        return reader.still_jpeg() if reader else None

    def _process(self, runner: _GateRunner, frame: np.ndarray, people: list[dict] | None = None, ts: float | None = None) -> None:
        """people=None: detect with the runner's own YOLOv8 + ByteTrack tracker."""
        try:
            result = reid_worker.process_frame(runner.camera_id, frame, runner.state, people=people, now=ts)
            if result:
                self._record(runner.camera_id, result, runner.generation)
        except Exception as e:
            # Most likely a model that failed to load, or a transient DB
            # error. Paused on a backoff (never permanently); after repeated
            # failures the gate's runner is rebuilt (fresh tracker, models
            # reloaded). The live video is unaffected either way.
            if self.health(runner.camera_id).failure(e):
                with self._lock:
                    if self._runners.get(runner.camera_id) is runner:
                        self._runners.pop(runner.camera_id, None)
                runner.executor.shutdown(wait=False)
        else:
            self.health(runner.camera_id).success()

    # --- turning track results into durable events ------------------------

    def _record(self, camera_id: int, result: dict, generation: int | None = None) -> None:
        now = time.time()
        with self._write_lock:
            if generation is not None and generation != self._generation:
                return  # computed before a reset — belongs to the old count
            learned = False
            for track in result["tracks"]:
                pending = track.get("pending_new_person")
                if pending:
                    person_id = self._enroll(camera_id, track, pending, now)
                    self._sighted[(camera_id, track["track_id"])] = (person_id, now)
                elif track["state"] == reid_db.TRACK_STATE_CONFIRMED and track["person_id"] is not None:
                    learned = self._learn_view(camera_id, track) or learned
                    key = (camera_id, track["track_id"])
                    prev = self._sighted.get(key)
                    if prev is None or prev[0] != track["person_id"]:
                        reid_db.log_event(camera_id, track["track_id"], reid_db.EVENT_SIGHTING,
                                          track["person_id"], track["confidence"], now)
                        reid_db.touch_person(track["person_id"], now)
                    self._sighted[key] = (track["person_id"], now)
            cutoff = now - reid_config.REID_TRACK_TIMEOUT_SECONDS * 3
            for key in [k for k, (_pid, seen) in self._sighted.items() if seen < cutoff]:
                del self._sighted[key]
            if learned:
                self._gallery.reload()

    def _learn_view(self, camera_id: int, track: dict) -> bool:
        """Adds this observation's embedding to the confirmed person when the
        gallery covered it poorly (a new angle, another gate), so the next
        time they're seen like this they match instead of becoming a second
        identity. Only for a confirmed track whose raw match agrees with the
        fused identity, and capped per person."""
        match = track.get("match")
        if not match or match["person_id"] != track["person_id"]:
            return False
        if match["score"] >= reid_config.REID_LEARN_BELOW_SIMILARITY:
            return False  # already well covered
        if reid_db.embedding_count_for_person(track["person_id"]) >= reid_config.REID_MAX_EMBEDDINGS_PER_PERSON:
            return False
        reid_db.add_embedding(track["person_id"], np.asarray(match["embedding"], dtype=np.float32),
                              match["quality_score"], source_camera=camera_id)
        return True

    # --- counting zone per gate -------------------------------------------

    def get_zone(self, camera_id: int) -> list | None:
        return reid_db.get_camera_config(camera_id).get("roi")

    def set_zone(self, camera_id: int, roi: list | None) -> None:
        """roi: polygon [[x, y], ...] in 0..1 frame fractions, or None to
        count the whole frame. A person counts only while the centre of
        their body box is inside it. The gate's runner is dropped so it's
        rebuilt with the new zone on the next frame."""
        cfg = reid_db.get_camera_config(camera_id)
        reid_db.set_camera_config(
            camera_id, enabled=True, similarity_threshold=cfg.get("similarity_threshold"),
            quality_min_score=cfg.get("quality_min_score"), mot_interval_seconds=cfg.get("mot_interval_seconds"),
            roi=roi,
        )
        with self._lock:
            self._runners.pop(camera_id, None)
        log.info("footfall: counting zone for camera %s set to %s", camera_id, roi or "whole frame")

    def _enroll(self, camera_id: int, track: dict, samples: list[dict], now: float) -> int:
        embeddings = [np.asarray(s["embedding"], dtype=np.float32) for s in samples]

        votes = Counter()
        for emb in embeddings:
            pid, _score, _margin = self._gallery.best_match(emb)
            if pid is not None:
                votes[pid] += 1
        if votes:
            pid, n = votes.most_common(1)[0]
            if n >= len(embeddings) * ENROLL_RECHECK_MATCH_FRACTION:
                reid_db.log_event(camera_id, track["track_id"], reid_db.EVENT_SIGHTING, pid, track["confidence"], now)
                reid_db.touch_person(pid, now)
                return pid

        person_id = reid_db.create_person(now=now)
        for s, emb in zip(samples, embeddings):
            reid_db.add_embedding(person_id, emb, s["quality_score"], source_camera=camera_id)
        best = max(samples, key=lambda s: s["quality_score"] or 0)
        if best.get("crop_jpeg"):
            reid_db.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            path = reid_db.SNAPSHOTS_DIR / f"person_{person_id}_{int(now)}.jpg"
            path.write_bytes(best["crop_jpeg"])
            reid_db.add_snapshot(person_id, camera_id, storage.to_stored(path), best["quality_score"])
        reid_db.log_event(camera_id, track["track_id"], reid_db.EVENT_NEW_PERSON, person_id, track["confidence"], now)
        self._gallery.reload()
        log.info("footfall: new person %s at camera %s", person_id, camera_id)
        return person_id

    # --- UAT: restart counting from zero ----------------------------------

    def reset(self) -> dict:
        """Wipes every Re-ID identity (people, embeddings, snapshots, events)
        so counting restarts at PERSON_001, and drops all in-memory tracking
        so nobody half-seen before the reset carries over. Cameras, faces,
        attendance etc. are untouched — see reid_db.reset_identities."""
        with self._write_lock:
            removed = reid_db.reset_identities(delete_snapshot_files=True)
            self._generation += 1
            self._sighted.clear()
            with self._lock:
                # Fresh fusion/pending state per gate; recreated lazily in feed().
                self._runners.clear()
            if self._gallery is not None:
                self._gallery.reload()
        log.info("footfall: count reset (%s people removed)", removed.get("reid_persons", 0))
        return {"people_removed": removed.get("reid_persons", 0), "reset_at": time.time()}

    def people(self, limit: int = 200) -> list[dict]:
        """Every identity currently in the registry, newest first, with the
        gates it was seen at and up to 3 snapshot ids for the UAT panel."""
        names = {c["id"]: c["name"] for c in config.cameras()}
        with reid_db.get_connection() as conn:
            rows = conn.execute(
                "SELECT p.id, p.label, p.first_seen, p.last_seen, "
                "(SELECT GROUP_CONCAT(DISTINCT e.camera_id) FROM reid_events e WHERE e.person_id = p.id), "
                "(SELECT COUNT(*) FROM reid_events e WHERE e.person_id = p.id) "
                "FROM reid_persons p ORDER BY p.first_seen DESC LIMIT ?", (limit,),
            ).fetchall()
        snaps = reid_db.list_snapshots_bulk([r[0] for r in rows], per_person=3)
        return [
            {
                "id": pid,
                "label": label,
                "first_seen": first,
                "last_seen": last,
                "gates": [names.get(int(c), f"Camera {c}") for c in (cams or "").split(",") if c],
                "sightings": sightings,
                "snapshot_ids": [s["id"] for s in snaps.get(pid, [])],
            }
            for pid, label, first, last, cams, sightings in rows
        ]

    @staticmethod
    def snapshot_path(snapshot_id: int):
        with reid_db.get_connection() as conn:
            row = conn.execute("SELECT file_path FROM reid_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if row is None:
            return None
        path = storage.resolve(row[0])
        return path if path is not None and path.is_file() else None

    # --- reporting --------------------------------------------------------

    def summary(self) -> dict:
        now = time.time()
        day_start = reid_db._day_start(now)
        gates = config.cameras()

        with reid_db.get_connection() as conn:
            per_gate = dict(conn.execute(
                "SELECT camera_id, COUNT(DISTINCT person_id) FROM reid_events "
                "WHERE person_id IS NOT NULL AND ts >= ? GROUP BY camera_id", (day_start,),
            ).fetchall())
            # Arrivals per hour = each person counted once, in the hour they
            # were FIRST seen that day (at any gate).
            hourly = {}
            for label, start, end in (("today", day_start, now + 1), ("yesterday", day_start - 86400, day_start)):
                hourly[label] = dict(conn.execute(
                    "SELECT CAST(strftime('%H', first_ts, 'unixepoch', 'localtime') AS INTEGER), COUNT(*) FROM ("
                    "  SELECT person_id, MIN(ts) AS first_ts FROM reid_events"
                    "  WHERE person_id IS NOT NULL AND ts >= ? AND ts < ? GROUP BY person_id"
                    ") GROUP BY 1", (start, end),
                ).fetchall())
            visitors = conn.execute(
                "SELECT p.label, MIN(e.ts), MAX(e.ts), GROUP_CONCAT(DISTINCT e.camera_id) "
                "FROM reid_events e JOIN reid_persons p ON p.id = e.person_id "
                "WHERE e.ts >= ? GROUP BY e.person_id ORDER BY MAX(e.ts) DESC LIMIT 200", (day_start,),
            ).fetchall()

        names = {c["id"]: c["name"] for c in gates}
        gate_rows = [
            {
                "camera_id": g["id"],
                "name": g["name"],
                "unique_today": per_gate.get(g["id"], 0),
                "counting": g["id"] in self._readers and not (g["id"] in self._health and self._health[g["id"]].failed),
                "stream": self._readers[g["id"]].state if g["id"] in self._readers else "OFFLINE",
                "stream_error": self._readers[g["id"]].last_error if g["id"] in self._readers else None,
                "state": self._health[g["id"]].state if g["id"] in self._health else "RUNNING",
                "zone": self.get_zone(g["id"]),
            }
            for g in gates
        ]
        # Hours 0-23 so the frontend chart has a stable x-axis; trimmed to
        # the span that actually has traffic (either day), min 8am-6pm.
        active_hours = [h for h in range(24) if hourly["today"].get(h) or hourly["yesterday"].get(h)]
        first_h = min(active_hours + [8])
        last_h = max(active_hours + [18])
        hourly_rows = [
            {"hour": h, "today": hourly["today"].get(h, 0), "yesterday": hourly["yesterday"].get(h, 0)}
            for h in range(first_h, last_h + 1)
        ]
        busiest = max(hourly["today"].items(), key=lambda kv: kv[1])[0] if hourly["today"] else None

        return {
            "unique_today": reid_db.count_unique_today(now),
            "new_today": reid_db.count_new_today(now),
            "returning_today": reid_db.count_returning_today(now),
            "avg_per_gate": round(sum(r["unique_today"] for r in gate_rows) / len(gate_rows), 1) if gate_rows else 0,
            "busiest_hour": busiest,
            "gates": gate_rows,
            "hourly": hourly_rows,
            "visitors": [
                {
                    "person": label,
                    "first_seen": first,
                    "last_seen": last,
                    "gates": [names.get(int(cid), f"Camera {cid}") for cid in (cams or "").split(",") if cid],
                }
                for label, first, last, cams in visitors
            ],
            "model_ready": bool(reid_config.REID_MODEL_PATH),
        }


service = FootfallService()
