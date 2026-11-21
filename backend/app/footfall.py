"""


Unique footfall across every entry gate, using the body-appearance Re-ID
engine vendored in app/reid/ (see unique-footfall-export/UNIQUE_FOOTFALL.md
for how the engine itself works and its real-world limits).

Gate cameras are simply cameras whose purpose is "Entry/Exit" — set it in
Camera Management, no separate config. All gates share ONE identity
gallery: a person first seen at gate 1 who leaves through gate 3, or comes
back in through gate 2, matches the same PERSON_NNN and is counted once.
That's the one real change from the export's own wiring, where every
camera worker held its own private gallery copy and only picked up other
cameras' new identities on an explicit reload signal. Here all gates run
in this one process, so they read the same gallery object, and it's
rebuilt the moment any gate enrolls someone.

Also owns keeping gate streams alive 24/7: CameraStream only reads RTSP
while it has a subscriber, and footfall has to count whether or not anyone
has the Live Feed page open. Same phantom-subscriber approach as
face_collection.py.
"""

import concurrent.futures
import datetime
import logging
import os
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

from . import analytics_settings, camera_db, camera_stream, clips, config, employee_directory, resilience, storage
from .reid import config as reid_config
from .reid import peopleid_gallery, reid_db, reid_worker
from .staff.tracker import StaffCameraTracker
from app import lifecycle  # noqa: E402

log = logging.getLogger("footfall")

# How often the gate list is re-read from the cameras table, so marking a
# camera "Entry/Exit" (or un-marking it) takes effect without a restart.
GATE_SYNC_INTERVAL_SECONDS = 15

# A track's pending_new_person samples are checked against the shared
# gallery once more before a new identity is created. If at least this
# fraction of them already match one existing person, it's that person —
# typically someone another gate enrolled in the seconds since this track's
# samples were collected.
ENROLL_RECHECK_MATCH_FRACTION = 0.5

# Evidence for each counted person: one ~10 s clip (5 s before + 5 s after the
# moment they were first counted that day, clips.py), instead of a still.
CLIPS_DIR = Path(config.DATA_DIR) / "footfall_clips"
MAX_CLIPS_DISK_MB = int(os.environ.get("FOOTFALL_MAX_CLIPS_DISK_MB", "2000"))
# Employee names: the head part of a counted person's box is identified by the
# camera's face-recognition pipeline (same rules as the live view), at most
# this often per person, until one confident match. Unknown otherwise.
IDENTIFY_EVERY_SECONDS = float(os.environ.get("FOOTFALL_IDENTIFY_EVERY_SECONDS", "3"))
HEAD_FRACTION = 0.4
# An entry whose person Re-ID hasn't recognised/enrolled within this long
# (they went through the door before enough good looks) is dropped.
ENTRY_RESOLVE_SECONDS = float(os.environ.get("FOOTFALL_ENTRY_RESOLVE_SECONDS", "60"))
UNKNOWN_NAME = "Person"   # never "Unknown": a name only when face recognition confirms an employee


def is_gate(cam: dict) -> bool:
    return "entry" in (cam.get("purpose") or "").lower()


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


class _KeepAliveSink:
    """Occupies a CameraStream subscriber slot so the RTSP loop keeps
    running with no viewer; drops every frame it's handed."""

    def put_nowait(self, item):
        pass


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
        # Gates with an entry line count only people who cross it from outside
        # to inside. The crossing rules are Staff Count's own line tracker
        # (staff/tracker.py), reused as is: it reports confirmed entries here.
        cfg = self.state.camera_config
        self.entries = _EntryRecorder()
        self.crosser = (StaffCameraTracker(camera_id, self.entries, cfg["entry_line"], cfg.get("inside_sign", 1), roi=cfg.get("roi"))
                        if cfg.get("entry_line") else None)


class _EntryRecorder:
    """Stands in for the staff occupancy manager behind StaffCameraTracker:
    keeps the confirmed ENTRIES (track id -> crossing time) and EXITS for
    footfall."""

    def __init__(self):
        self.pending: dict[int, float] = {}
        self.exits: list[tuple[int, float]] = []

    def entry(self, camera_id, track_id, employee_id=None, confidence=None, embedding=None, ts=None):
        self.pending.setdefault(track_id, ts if ts is not None else time.time())
        return {"event_type": "ENTRY", "visit_id": None}

    def exit(self, camera_id, track_id, employee_id=None, confidence=None, embedding=None, ts=None, visit_id=None):
        self.exits.append((track_id, ts if ts is not None else time.time()))
        return {"event_type": "EXIT", "visit_id": None}

    def take_exits(self) -> list[tuple[int, float]]:
        out, self.exits = self.exits, []
        return out

    def take(self) -> dict[int, float]:
        out, self.pending = self.pending, {}
        return out


class FootfallService:
    def __init__(self):
        self._gallery: _SharedGallery | None = None
        self._runners: dict[int, _GateRunner] = {}
        # Per-gate failure tracking that outlives a rebuilt runner (see _process).
        self._health: dict[int, resilience.FeatureHealth] = {}
        self._sinks: dict[int, _KeepAliveSink] = {}
        self._lock = threading.Lock()
        # Serializes every reid_db write + gallery rebuild across gates, so
        # two gates can't both enroll the same newly-arrived person.
        self._write_lock = threading.Lock()
        # (camera_id, track_id) -> (person_id, last_seen): one sighting
        # event per person per track, not one per processed frame.
        self._sighted: dict[tuple[int, int], tuple[int, float]] = {}
        self._generation = 0
        self._started = False
        # (person_id, day) already given their clip.
        self._clipped: set[tuple[int, str]] = set()
        # Per track, key (camera_id, track_id):
        self._entered: dict[tuple[int, int], tuple[float, list | None]] = {}   # crossed in, not yet counted: (when, bbox)
        self._track_person: dict[tuple[int, int], tuple[int, bool, float, float]] = {}  # (person, created here, conf, last seen)
        self._track_employee: dict[tuple[int, int], tuple[str, float]] = {}  # face recognition's answer
        self._track_counted: dict[tuple[int, int], tuple[int, str]] = {}     # -> (person, day) it was counted as
        self._identify_tried: dict[tuple[int, int], float] = {}
        # (person_id, day) already named: their other tracks need no more face work.
        self._employee_days: set[tuple[int, str]] = set()
        self._identify_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="footfall-identify")
        self._identify_future: concurrent.futures.Future | None = None

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
        threading.Thread(target=self._sync_loop, daemon=True, name="footfall-gate-sync").start()

    def _sync_loop(self) -> None:
        while True:
            try:
                self._sync_gates()
            except Exception:
                log.exception("footfall: gate sync failed")
            if lifecycle.wait(GATE_SYNC_INTERVAL_SECONDS):
                return
    def _sync_gates(self) -> None:
        # Switched off: stop keeping gates streaming too, so they cost nothing.
        gate_ids = {c["id"] for c in camera_db.list_cameras() if is_gate(c) and camera_db.is_streamable(c)} if analytics_settings.enabled("footfall") else set()
        with self._lock:
            added = gate_ids - self._sinks.keys()
            removed = self._sinks.keys() - gate_ids
            for cid in added:
                sink = _KeepAliveSink()
                camera_stream.get_stream(cid).subscribe(sink, is_collector=True, wants_frames=False)
                self._sinks[cid] = sink
            for cid in removed:
                camera_stream.get_stream(cid).unsubscribe(self._sinks.pop(cid))
                self._runners.pop(cid, None)
        clips.recorder.want("footfall", gate_ids)  # rolling buffer for the counted-person clips
        for cid in added:
            log.info("footfall: counting on gate camera %s", cid)
        for cid in removed:
            log.info("footfall: camera %s is no longer a gate, stopped counting", cid)

    # --- tracked people from the shared detector (person_detection.py) -----

    def wants(self, camera_id: int) -> float:
        """Frames per second footfall wants from this camera (0 = none)."""
        if self._gallery is None or camera_id not in self._sinks or not analytics_settings.enabled("footfall"):
            return 0.0
        if not self.health(camera_id).allow():
            return 0.0
        return 1.0 / reid_config.REID_MOT_INTERVAL_SECONDS

    def health(self, camera_id: int) -> resilience.FeatureHealth:
        h = self._health.get(camera_id)
        if h is None:
            h = self._health.setdefault(camera_id, resilience.health_for("footfall", camera_id))
        return h

    def deliver(self, camera_id: int, frame: np.ndarray, people: list[dict], ts: float) -> None:
        runner = self._runners.get(camera_id)
        if runner is None:
            with self._lock:
                runner = self._runners.setdefault(camera_id, _GateRunner(camera_id, self._gallery, self._generation))
        if runner.future is not None and not runner.future.done():
            return  # previous look still being processed: skip this one
        runner.future = runner.executor.submit(self._process, runner, frame, people, ts)

    def _process(self, runner: _GateRunner, frame: np.ndarray, people: list[dict] | None = None, ts: float | None = None) -> None:
        """people=None: detect here with the runner's own tracker (tests)."""
        try:
            ts = ts if ts is not None else time.time()
            result = reid_worker.process_frame(runner.camera_id, frame, runner.state, people=people, now=ts)
            entries = None
            crosser = getattr(runner, "crosser", None)   # None: no entry line at this gate
            if crosser is not None:
                # Every tracked person takes part in the crossing check (it uses
                # the gate's area itself), and it runs on every look so a track
                # that vanished through the door is confirmed.
                tracked = [p for p in (people or []) if p.get("track_id") is not None]
                crosser.update(tracked, frame.shape, ts)
                entries = {tid: (when, next((p["bbox"] for p in tracked if p["track_id"] == tid), None))
                           for tid, when in runner.entries.take().items()}
                exits = runner.entries.take_exits()
                if exits:
                    self._record_exits(runner.camera_id, exits, runner.generation)
            if result or entries:
                counted = self._record(runner.camera_id, result or {"tracks": []}, runner.generation, entries)
                self._evidence(runner.camera_id, frame, counted, (result or {"tracks": []})["tracks"], ts)
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

    def _record(self, camera_id: int, result: dict, generation: int | None = None,
                entries: dict | None = None) -> list[tuple[int, list | None, float, tuple[int, int]]]:
        """Turns this look into counting events. Returns the people counted now:
        [(person_id, bbox | None, when, (camera_id, track_id))].

        entries None (no entry line at this gate): everyone Re-ID confirms in
        the counting area is counted, as before entry lines existed.
        entries {track_id: (when, bbox)}: only a person whose track crossed the
        entry line from outside to inside is counted, at that crossing. Re-ID
        still learns everyone near the gate (that is how a person is recognised
        when they do come in), but people who only walk or stand outside are
        never counted, and their identity never shows in the numbers."""
        now = time.time()
        counted = []
        line_mode = entries is not None
        with self._write_lock:
            if generation is not None and generation != self._generation:
                return []  # computed before a reset — belongs to the old count
            learned = False
            for track in result["tracks"]:
                key = (camera_id, track["track_id"])
                pending = track.get("pending_new_person")
                if pending:
                    person_id, created = self._enroll(camera_id, track, pending, now, log_event=not line_mode)
                    self._track_person[key] = (person_id, created, track["confidence"], now)
                    if not line_mode:
                        self._sighted[key] = (person_id, now)
                        counted.append((person_id, track.get("bbox"), now, key))
                elif track["state"] == reid_db.TRACK_STATE_CONFIRMED and track["person_id"] is not None:
                    learned = self._learn_view(camera_id, track) or learned
                    prev = self._track_person.get(key)
                    created = bool(prev and prev[0] == track["person_id"] and prev[1])
                    self._track_person[key] = (track["person_id"], created, track["confidence"], now)
                    if line_mode:
                        continue
                    counted.append((track["person_id"], track.get("bbox"), now, key))
                    prev = self._sighted.get(key)
                    if prev is None or prev[0] != track["person_id"]:
                        reid_db.log_event(camera_id, track["track_id"], reid_db.EVENT_SIGHTING,
                                          track["person_id"], track["confidence"], now)
                        reid_db.touch_person(track["person_id"], now)
                    self._sighted[key] = (track["person_id"], now)
            if line_mode:
                for tid, (when, bbox) in entries.items():
                    self._entered.setdefault((camera_id, tid), (when, bbox))
                counted = self._count_entries(camera_id, now)
            cutoff = now - reid_config.REID_TRACK_TIMEOUT_SECONDS * 3
            for key in [k for k, (_pid, seen) in self._sighted.items() if seen < cutoff]:
                del self._sighted[key]
            stale = now - max(ENTRY_RESOLVE_SECONDS, reid_config.REID_TRACK_TIMEOUT_SECONDS * 3)
            for key in [k for k, v in self._track_person.items() if v[3] < stale]:
                self._track_person.pop(key, None)
                self._track_employee.pop(key, None)
                self._track_counted.pop(key, None)
                self._identify_tried.pop(key, None)
            if learned:
                self._gallery.reload()
        return counted

    def _record_exits(self, camera_id: int, exits: list[tuple[int, float]], generation: int | None) -> None:
        """Outward crossings at an entry-line gate: an anonymous event each
        (no person), so they never touch the unique counts."""
        with self._write_lock:
            if generation is not None and generation != self._generation:
                return
            for track_id, when in exits:
                reid_db.log_event(camera_id, track_id, reid_db.EVENT_EXIT, None, None, when)

    def _count_entries(self, camera_id: int, now: float) -> list:
        """Counts each entered track once its person is known: an event at the
        crossing time (new_person if that track created the identity and it was
        never counted before, else a sighting). Called with _write_lock held."""
        counted = []
        for key, (when, bbox) in list(self._entered.items()):
            if key[0] != camera_id:
                continue
            tp = self._track_person.get(key)
            if tp is None:
                if now - when > ENTRY_RESOLVE_SECONDS:
                    del self._entered[key]
                    log.info("footfall: entry of track %s at camera %s dropped: never recognised well enough", key[1], camera_id)
                continue
            del self._entered[key]
            person_id, created, confidence, _seen = tp
            first_ever = created and not reid_db.has_events(person_id)
            reid_db.log_event(camera_id, key[1], reid_db.EVENT_NEW_PERSON if first_ever else reid_db.EVENT_SIGHTING,
                              person_id, confidence, when)
            reid_db.touch_person(person_id, when)
            log.info("footfall: person %s entered through gate camera %s", person_id, camera_id)
            counted.append((person_id, bbox, when, key))
        return counted

    # --- evidence: one clip per person per day, and their employee name -----

    def _evidence(self, camera_id: int, frame: np.ndarray, counted: list, look_tracks: list[dict], ts: float) -> None:
        """A clip for each newly counted person (once a day), and face
        identification of the people near the gate. Identification starts
        BEFORE they are counted: walking towards the gate is when the face is
        visible; the answer is attached when they are counted."""
        for person_id, _bbox, when, key in counted:
            day = datetime.datetime.fromtimestamp(when).strftime("%Y-%m-%d")
            self._track_counted[key] = (person_id, day)
            if (person_id, day) not in self._clipped:
                self._clipped.add((person_id, day))
                self._start_clip(camera_id, person_id, day, when)
            known = self._track_employee.get(key)
            if known:
                self._set_employee(person_id, day, *known)
        fut = self._identify_future
        if fut is not None and not fut.done():
            return  # one identification at a time; never queue
        today = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        for t in look_tracks:
            key = (camera_id, t["track_id"])
            if key in self._track_employee or not t.get("bbox") or ts - self._identify_tried.get(key, 0.0) < IDENTIFY_EVERY_SECONDS:
                continue
            person = self._track_person.get(key)
            if person and (person[0], today) in self._employee_days:
                continue  # this person is already named today (another track, another gate)
            self._identify_tried[key] = ts
            self._identify_future = self._identify_executor.submit(self._identify, camera_id, frame, t["bbox"], key)
            return

    def _start_clip(self, camera_id: int, person_id: int, day: str, ts: float) -> None:
        """First count of this person today: a row, and a clip around ts (the
        moment they entered). A row already there (a restart, another gate
        first) means no second clip."""
        with reid_db.get_connection() as conn:
            cur = conn.execute("INSERT OR IGNORE INTO footfall_clips (person_id, day, camera_id, ts) VALUES (?, ?, ?, ?)",
                               (person_id, day, camera_id, ts))
            if cur.rowcount == 0:
                return
            clip_id = cur.lastrowid
        final = CLIPS_DIR / str(camera_id) / f"person_{person_id}_{clip_id}.mp4"

        def done(status, frames=None, error=None):
            with reid_db.get_connection() as conn:
                if status == "ready":
                    cur = conn.execute("UPDATE footfall_clips SET clip_status = 'ready', clip_path = ?, clip_frames = ? WHERE id = ?",
                                       (final.relative_to(Path(config.DATA_DIR)).as_posix(), frames, clip_id))
                    if cur.rowcount == 0:
                        final.unlink(missing_ok=True)  # reset while encoding
                else:
                    conn.execute("UPDATE footfall_clips SET clip_status = 'failed', clip_error = ? WHERE id = ?", (error, clip_id))

        clips.recorder.schedule(camera_id, ts, final, done, max_dir_mb=MAX_CLIPS_DISK_MB)

    def _set_employee(self, person_id: int, day: str, employee: str, confidence: float) -> None:
        self._employee_days.add((person_id, day))
        with reid_db.get_connection() as conn:
            conn.execute("UPDATE footfall_clips SET employee_id = ?, employee_confidence = ? "
                         "WHERE person_id = ? AND day = ? AND employee_id IS NULL",
                         (employee, confidence, person_id, day))

    def _identify(self, camera_id: int, frame: np.ndarray, bbox, key: tuple[int, int]) -> None:
        """The registered employee on this track, if the camera's face pipeline
        can tell (its live-view rules: classifier, then the enrolment gallery,
        each with its threshold). No confident match = no name, never a guess."""
        from app import face_pipeline

        try:
            pipe = face_pipeline.get_existing_pipeline(camera_id)
            if pipe is None:
                return  # face recognition is not running on this camera
            x1, y1, x2, y2 = bbox
            employee, confidence, outcome = pipe._identify_head(frame, [x1, y1, x2, y1 + HEAD_FRACTION * (y2 - y1)])  # noqa: SLF001
            if face_pipeline.FACE_DIAG:
                log.info("face_diag footfall camera=%s track=%s head_px=%dx%d -> %s %s (%.2f)", camera_id, key[1],
                         int(x2 - x1), int(HEAD_FRACTION * (y2 - y1)), outcome, employee or "", confidence)
            if not employee:
                return
            self._track_employee[key] = (str(employee), float(confidence))
            counted = self._track_counted.get(key)
            if counted:
                self._set_employee(counted[0], counted[1], str(employee), float(confidence))
            log.info("footfall: track %s at camera %s is employee %s", key[1], camera_id, employee)
        except Exception:
            log.exception("footfall: could not identify track %s at camera %s", key[1], camera_id)

    @staticmethod
    def clip_file(clip_id: int) -> tuple[Path | None, str]:
        """(path, reason): reason is ok | missing | pending | failed | expired | file_missing."""
        with reid_db.get_connection() as conn:
            row = conn.execute("SELECT clip_path, clip_status FROM footfall_clips WHERE id = ?", (clip_id,)).fetchone()
        if row is None:
            return None, "missing"
        path, status = row
        if status != "ready" or not path:
            return None, status if status in ("pending", "expired") else "failed"
        p = (Path(config.DATA_DIR) / path).resolve()
        if p.parent.parent != CLIPS_DIR.resolve():
            return None, "missing"   # never serve outside the clips folder
        return (p, "ok") if p.is_file() else (None, "file_missing")

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

    def set_entry_line(self, camera_id: int, line: list | None, inside_sign: int = 1) -> None:
        """The line across the gate and which side is inside. With it, only
        people who cross from outside to inside are counted; None goes back to
        counting everyone in the area. The runner is rebuilt to pick it up."""
        reid_db.set_entry_line(camera_id, line, inside_sign)
        with self._lock:
            self._runners.pop(camera_id, None)
        log.info("footfall: entry line for camera %s %s", camera_id, f"set ({'inside +' if inside_sign >= 0 else 'inside -'})" if line else "removed")

    @staticmethod
    def get_entry(camera_id: int) -> dict:
        cfg = reid_db.get_camera_config(camera_id)
        return {"entry_line": cfg.get("entry_line"), "inside_sign": cfg.get("inside_sign", 1)}

    def _enroll(self, camera_id: int, track: dict, samples: list[dict], now: float, log_event: bool = True) -> tuple[int, bool]:
        """Recognises or creates the identity for a track's samples. Returns
        (person_id, created). log_event=False (entry-line gates): no counting
        event here; the person is counted only when they enter."""
        embeddings = [np.asarray(s["embedding"], dtype=np.float32) for s in samples]

        votes = Counter()
        for emb in embeddings:
            pid, _score, _margin = self._gallery.best_match(emb)
            if pid is not None:
                votes[pid] += 1
        if votes:
            pid, n = votes.most_common(1)[0]
            if n >= len(embeddings) * ENROLL_RECHECK_MATCH_FRACTION:
                if log_event:
                    reid_db.log_event(camera_id, track["track_id"], reid_db.EVENT_SIGHTING, pid, track["confidence"], now)
                    reid_db.touch_person(pid, now)
                return pid, False

        person_id = reid_db.create_person(now=now)
        for s, emb in zip(samples, embeddings):
            reid_db.add_embedding(person_id, emb, s["quality_score"], source_camera=camera_id)
        best = max(samples, key=lambda s: s["quality_score"] or 0)
        if best.get("crop_jpeg"):
            reid_db.SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            path = reid_db.SNAPSHOTS_DIR / f"person_{person_id}_{int(now)}.jpg"
            path.write_bytes(best["crop_jpeg"])
            reid_db.add_snapshot(person_id, camera_id, storage.to_stored(path), best["quality_score"])
        if log_event:
            reid_db.log_event(camera_id, track["track_id"], reid_db.EVENT_NEW_PERSON, person_id, track["confidence"], now)
        self._gallery.reload()
        log.info("footfall: new person %s at camera %s%s", person_id, camera_id, "" if log_event else " (counted only if they enter)")
        return person_id, True

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
            self._clipped.clear()
            self._entered.clear()
            self._track_person.clear()
            self._track_employee.clear()
            self._track_counted.clear()
            self._identify_tried.clear()
            self._employee_days.clear()
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
        names = {c["id"]: c["name"] for c in camera_db.list_cameras()}
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
        from app import retention

        with reid_db.get_connection() as conn:
            row = conn.execute("SELECT file_path, created_at FROM reid_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if row is None:
            return None
        # Not served past its lifetime, even if the purge has not reached the file yet.
        if retention.FOOTFALL_PICTURE_HOURS > 0 and time.time() - row[1] > retention.FOOTFALL_PICTURE_HOURS * 3600:
            return None
        path = storage.resolve(row[0])
        return path if path is not None and path.is_file() else None

    # --- reporting --------------------------------------------------------

    def day_stats(self, day: str) -> dict:
        """One local day's footfall: entries (unique people counted, each once),
        exits (outward crossings at entry-line gates), employee vs visitor
        entries, arrivals per hour and the peak hour, and the previous day's
        entries for the trend."""
        start = datetime.datetime.strptime(day, "%Y-%m-%d")
        t0, t1 = start.timestamp(), (start + datetime.timedelta(days=1)).timestamp()
        prev = (start - datetime.timedelta(days=1)).timestamp()
        with reid_db.get_connection() as conn:
            entries = conn.execute("SELECT COUNT(DISTINCT person_id) FROM reid_events WHERE person_id IS NOT NULL AND ts >= ? AND ts < ?", (t0, t1)).fetchone()[0]
            prev_entries = conn.execute("SELECT COUNT(DISTINCT person_id) FROM reid_events WHERE person_id IS NOT NULL AND ts >= ? AND ts < ?", (prev, t0)).fetchone()[0]
            exits = conn.execute("SELECT COUNT(*) FROM reid_events WHERE event_type = ? AND ts >= ? AND ts < ?", (reid_db.EVENT_EXIT, t0, t1)).fetchone()[0]
            hourly = dict(conn.execute(
                "SELECT CAST(strftime('%H', first_ts, 'unixepoch', 'localtime') AS INTEGER), COUNT(*) FROM ("
                "  SELECT person_id, MIN(ts) AS first_ts FROM reid_events WHERE person_id IS NOT NULL AND ts >= ? AND ts < ? GROUP BY person_id"
                ") GROUP BY 1", (t0, t1)).fetchall())
            employees = conn.execute("SELECT COUNT(*) FROM footfall_clips WHERE day = ? AND employee_id IS NOT NULL", (day,)).fetchone()[0]
        employees = min(employees, entries)
        gates = [c for c in camera_db.list_cameras() if is_gate(c)]
        return {
            "entries": entries,
            "previous_day_entries": prev_entries,
            "exits": exits,
            "employee_entries": employees,
            "visitor_entries": entries - employees,
            "hourly": [{"hour": h, "entries": hourly[h]} for h in sorted(hourly)],
            "peak_hour": max(hourly, key=hourly.get) if hourly else None,
            "gates": len(gates),
            "gates_without_entry_line": sum(1 for g in gates if not self.get_entry(g["id"])["entry_line"]),
        }

    def summary(self) -> dict:
        now = time.time()
        day_start = reid_db._day_start(now)
        gates = [c for c in camera_db.list_cameras() if is_gate(c)]

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
                "SELECT p.label, MIN(e.ts), MAX(e.ts), GROUP_CONCAT(DISTINCT e.camera_id), p.id "
                "FROM reid_events e JOIN reid_persons p ON p.id = e.person_id "
                "WHERE e.ts >= ? GROUP BY e.person_id ORDER BY MAX(e.ts) DESC LIMIT 200", (day_start,),
            ).fetchall()
            # Newest picture still within its 48 h lifetime, per person.
            from app import retention

            picture_cutoff = now - retention.FOOTFALL_PICTURE_HOURS * 3600 if retention.FOOTFALL_PICTURE_HOURS > 0 else 0
            pictures: dict[int, int] = {}
            ids = [v[4] for v in visitors]
            if ids:
                for pid, sid in conn.execute(
                    f"SELECT person_id, id FROM reid_snapshots WHERE created_at >= ? AND person_id IN ({','.join('?' * len(ids))}) "
                    "ORDER BY created_at ASC", (picture_cutoff, *ids)):
                    pictures[pid] = sid  # ascending, so the last one wins = the newest
            # Today's clip and employee identity per person.
            evidence: dict[int, tuple] = {}
            if ids:
                today = datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
                for row in conn.execute(
                    f"SELECT person_id, id, camera_id, ts, employee_id, clip_status, clip_error FROM footfall_clips "
                    f"WHERE day = ? AND person_id IN ({','.join('?' * len(ids))})", (today, *ids)):
                    evidence[row[0]] = row[1:]

        names = {c["id"]: c["name"] for c in camera_db.list_cameras()}

        def identity(pid: int) -> dict:
            ev = evidence.get(pid)
            if ev is None:
                return {"name": UNKNOWN_NAME, "employee_id": None, "clip_id": None, "clip_status": None,
                        "clip_error": None, "clip_camera": None, "clip_at": None}
            clip_id, cam, ts, employee_id, status, error = ev
            name = (employee_directory.get_display(employee_id)[0] or employee_id) if employee_id else UNKNOWN_NAME
            return {"name": name, "employee_id": employee_id, "clip_id": clip_id, "clip_status": status,
                    "clip_error": error, "clip_camera": names.get(cam, f"Camera {cam}"), "clip_at": ts}
        gate_rows = [
            {
                "camera_id": g["id"],
                "name": g["name"],
                "unique_today": per_gate.get(g["id"], 0),
                "counting": g["id"] in self._sinks and not (g["id"] in self._health and self._health[g["id"]].failed),
                "state": self._health[g["id"]].state if g["id"] in self._health else "RUNNING",
                "zone": self.get_zone(g["id"]),
                **self.get_entry(g["id"]),   # entry_line None = counts everyone in the area (no entry rule yet)
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

        unique_today = reid_db.count_unique_today(now)
        # KPI cards: of today's entries, how many face recognition confirmed as
        # registered employees; everyone else is a visitor entry.
        with reid_db.get_connection() as conn:
            employee_entries = conn.execute(
                "SELECT COUNT(*) FROM footfall_clips WHERE day = ? AND employee_id IS NOT NULL",
                (datetime.datetime.fromtimestamp(now).strftime("%Y-%m-%d"),)).fetchone()[0]
        employee_entries = min(employee_entries, unique_today)
        return {
            "unique_today": unique_today,
            "employee_entries": employee_entries,
            "visitor_entries": unique_today - employee_entries,
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
                    "snapshot_id": pictures.get(pid),  # None once the picture's 48 h are up
                    **identity(pid),   # name ("Person" unless recognised as an employee), clip
                }
                for label, first, last, cams, pid in visitors
            ],
            "model_ready": bool(reid_config.REID_MODEL_PATH),
        }


service = FootfallService()

from app import person_detection  # noqa: E402

person_detection.service.register("footfall", service.wants, service.deliver)
