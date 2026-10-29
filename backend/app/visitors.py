"""
visitors.py

Visitor Management: an alert when an UNKNOWN person sits in an area an admin
marked on a camera (the visitor seating area), with a 10-second video clip. It
works on tracked people (person_detection.py, the same detector footfall / staff
count share, so this adds no second detection pass; cameras without a saved area
cost nothing). Employees are recognised with the face pipeline's existing
identities, so this adds no face inference either.

Zone rule      A person is "in the area" when their FEET (bottom-centre of the box)
               are inside the polygon, as fractions of the frame, so the saved
               area is independent of how the picture is displayed.
Visit          Inside on ENTRY_CONFIRM_LOOKS consecutive looks (about 1 s) opens a
               visit. A person who stays inside, and the tracker handing them a new
               id at the same spot (within ABSENCE_GRACE_SECONDS), stay one visit.
Employees      Each look, the visitor's box is matched (overlap) to the face
               pipeline's live tracks on the same camera. A track that pipeline has
               identified as an enrolled person (EMPLOYEE_SOURCES: by face, and by
               body appearance matched to someone face-recognised today) makes the
               visit an EMPLOYEE visit: no alert, not counted. If that happens after
               an alert, the alert and its clip are removed again.
Alerts         Per visit, never per area. An alert is due at entry, then every
               REPEAT_ALERT_MINUTES (30). A due alert fires only once the person has
               been seen without a break (gaps up to PRESENCE_BREAK_SECONDS are not
               a break) for DETECTION_GAP_SECONDS (10) since it became due, and is
               still not recognised as an employee. So the first alert comes ~10 s
               after sitting down (enough for recognition to name an employee), and
               a person still seated at 11:30 is alerted again at ~11:30:10, not
               every 10 s. A brief detection loss neither resets the 30-minute timer
               nor raises an extra alert.
Departure      Not in the area for ABSENCE_GRACE_SECONDS (60): the visit closes and
               its alerts are marked "left". Coming back after that is a new visit
               with a new alert cycle.
Clip           A rolling buffer of the last few seconds per camera that has an
               area; when an alert fires, ~5 s before + ~5 s after it is
               encoded to an H.264 MP4 on a background thread (live video and
               detection never wait for it).
Retention      Every event carries an expiry, so nothing is kept forever:
                 expires_at = departure + RETENTION_AFTER_DEPARTURE_HOURS (24 h)
               and if the departure is never established, at the latest
                 entry + MAX_PRESENCE_HOURS + RETENTION_AFTER_DEPARTURE_HOURS (36 h).
               A backend job deletes the clip file and the event row after that;
               old links then answer 404/410. Areas live in their own table and
               are never touched by this clean-up.
"""

from __future__ import annotations

import collections
import concurrent.futures
import itertools
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from app import analytics_settings, camera_db, config, db, lifecycle, person_detection, resilience
from app.desk_tracker import in_polygon

log = logging.getLogger("visitors")

DB_PATH = config.DB_PATH
CLIPS_DIR = Path(config.DATA_DIR) / "visitor_clips"

DETECT_FPS = float(os.environ.get("VISITOR_DETECT_FPS", "2"))
PERSON_CONF = float(os.environ.get("VISITOR_PERSON_CONF", "0.45"))
ENTRY_CONFIRM_LOOKS = int(os.environ.get("VISITOR_ENTRY_CONFIRM_LOOKS", "2"))
# Alert policy (see the module docstring). Each is per visit.
DETECTION_GAP_SECONDS = float(os.environ.get("VISITOR_DETECTION_GAP_SECONDS", "10"))
REPEAT_ALERT_MINUTES = float(os.environ.get("VISITOR_REPEAT_ALERT_MINUTES", "30"))  # 0 = alert once per visit
PRESENCE_BREAK_SECONDS = float(os.environ.get("VISITOR_PRESENCE_BREAK_SECONDS", "3"))
ABSENCE_GRACE_SECONDS = float(os.environ.get("VISITOR_ABSENCE_GRACE_SECONDS",
                                             os.environ.get("VISITOR_EXIT_GRACE_SECONDS", "60")))
EXIT_GRACE_SECONDS = ABSENCE_GRACE_SECONDS  # older name, kept for the settings API
EXCLUDE_EMPLOYEES = os.environ.get("VISITOR_EXCLUDE_EMPLOYEES", "1").strip().lower() not in ("0", "false", "no", "off")
EMPLOYEE_SOURCES = frozenset(s.strip() for s in os.environ.get("VISITOR_EMPLOYEE_SOURCES", "face,appearance").split(",") if s.strip())
EMPLOYEE_MATCH_IOU = 0.3
ID_SWITCH_SECONDS = 3.0   # a new track id this soon after, overlapping, is the same person
ID_SWITCH_IOU = 0.3

PRE_SECONDS = float(os.environ.get("VISITOR_CLIP_PRE_SECONDS", "5"))
POST_SECONDS = float(os.environ.get("VISITOR_CLIP_POST_SECONDS", "5"))
BUFFER_FPS = float(os.environ.get("VISITOR_BUFFER_FPS", "6"))
BUFFER_WIDTH = int(os.environ.get("VISITOR_BUFFER_WIDTH", "640"))
BUFFER_SECONDS = PRE_SECONDS + POST_SECONDS + 4.0
MAX_CLIPS_DISK_MB = int(os.environ.get("VISITOR_MAX_CLIPS_DISK_MB", "2000"))

RETENTION_AFTER_DEPARTURE_HOURS = float(os.environ.get("VISITOR_RETENTION_AFTER_DEPARTURE_HOURS", "24"))
MAX_PRESENCE_HOURS = float(os.environ.get("VISITOR_MAX_PRESENCE_HOURS", "12"))
PURGE_INTERVAL_SECONDS = 300
HOUSEKEEPING_SECONDS = 5
LAST_IN_WRITE_SECONDS = 5

FEATURE = "visitors"


def _conn():
    """Shared connection policy (app/db.py)."""
    return db.connect(DB_PATH, row_factory=sqlite3.Row)


def init_db() -> None:
    with _conn() as conn:
        # PERMANENT configuration: one saved area per camera.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS visitor_zones (
                   camera_id INTEGER PRIMARY KEY,
                   polygon TEXT NOT NULL,            -- JSON [[x, y], ...] fractions of the frame
                   enabled INTEGER NOT NULL DEFAULT 1,
                   updated_at REAL NOT NULL)"""
        )
        # TEMPORARY data: removed (row and clip file) once expires_at passes.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS visitor_events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   camera_id INTEGER NOT NULL,
                   camera_name TEXT,
                   track_id INTEGER,
                   entered_at REAL NOT NULL,
                   last_in_zone_at REAL NOT NULL,
                   left_at REAL,
                   departure TEXT,                   -- tracked | assumed | NULL while inside
                   status TEXT NOT NULL DEFAULT 'inside',   -- inside | left
                   clip_path TEXT,
                   clip_status TEXT NOT NULL DEFAULT 'pending',  -- pending | ready | failed
                   clip_error TEXT,
                   clip_frames INTEGER,
                   expires_at REAL NOT NULL,
                   created_at REAL NOT NULL,
                   entry_x REAL,                     -- where the visitor's feet were at entry (fractions of the frame)
                   entry_y REAL)"""
        )
        # Databases created before these columns existed. One row is one ALERT;
        # visit_key groups the alerts of one continuous stay (1st, 2nd after
        # 30 min, ...), alert_at is when that alert fired.
        for col, typ in (("entry_x", "REAL"), ("entry_y", "REAL"), ("visit_key", "TEXT"),
                         ("alert_seq", "INTEGER"), ("alert_at", "REAL")):
            try:
                conn.execute(f"ALTER TABLE visitor_events ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor_events_cam ON visitor_events (camera_id, entered_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor_events_visit ON visitor_events (visit_key)")
        # PERMANENT report history: one row per alert, times and camera only (no
        # clip, no image), so reports outlive the 24 h clip retention. Removed
        # only with its alert (an employee recognised late, or a manual delete).
        conn.execute(
            """CREATE TABLE IF NOT EXISTS visitor_report_log (
                   event_id INTEGER PRIMARY KEY,
                   camera_id INTEGER NOT NULL,
                   camera_name TEXT,
                   visit_key TEXT,
                   alert_seq INTEGER NOT NULL DEFAULT 1,
                   entered_at REAL NOT NULL,
                   alert_at REAL NOT NULL,
                   left_at REAL,
                   departure TEXT)"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor_report_log_ts ON visitor_report_log (alert_at)")
        # Alerts recorded before this table existed (still within retention).
        conn.execute(
            "INSERT OR IGNORE INTO visitor_report_log (event_id, camera_id, camera_name, visit_key, alert_seq, entered_at, alert_at, left_at, departure) "
            "SELECT id, camera_id, camera_name, visit_key, COALESCE(alert_seq, 1), entered_at, COALESCE(alert_at, entered_at), left_at, departure "
            "FROM visitor_events"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor_events_exp ON visitor_events (expires_at)")


# --------------------------------------------------------------------------- zones

def _check_polygon(polygon) -> list[list[float]]:
    if not isinstance(polygon, list) or len(polygon) < 3:
        raise ValueError("The area needs at least 3 points")
    out = []
    for p in polygon:
        if not isinstance(p, (list, tuple)) or len(p) != 2 or not all(isinstance(v, (int, float)) for v in p):
            raise ValueError("Each point must be [x, y]")
        if not (0 <= p[0] <= 1 and 0 <= p[1] <= 1):
            raise ValueError("Points must be between 0 and 1 (fractions of the picture)")
        out.append([round(float(p[0]), 5), round(float(p[1]), 5)])
    return out


def get_zone(camera_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM visitor_zones WHERE camera_id = ?", (camera_id,)).fetchone()
    return None if row is None else {"camera_id": row["camera_id"], "polygon": json.loads(row["polygon"]),
                                      "enabled": bool(row["enabled"]), "updated_at": row["updated_at"]}


def list_zones() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM visitor_zones").fetchall()
    return [{"camera_id": r["camera_id"], "polygon": json.loads(r["polygon"]), "enabled": bool(r["enabled"]),
             "updated_at": r["updated_at"]} for r in rows]


def save_zone(camera_id: int, polygon, enabled: bool = True) -> dict:
    polygon = _check_polygon(polygon)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO visitor_zones (camera_id, polygon, enabled, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(camera_id) DO UPDATE SET polygon = excluded.polygon, enabled = excluded.enabled, updated_at = excluded.updated_at",
            (camera_id, json.dumps(polygon), 1 if enabled else 0, time.time()),
        )
    service.refresh()
    return get_zone(camera_id)


def set_zone_enabled(camera_id: int, enabled: bool) -> dict | None:
    with _conn() as conn:
        cur = conn.execute("UPDATE visitor_zones SET enabled = ?, updated_at = ? WHERE camera_id = ?",
                           (1 if enabled else 0, time.time(), camera_id))
        if cur.rowcount == 0:
            return None
    service.refresh()
    return get_zone(camera_id)


def delete_zone(camera_id: int) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM visitor_zones WHERE camera_id = ?", (camera_id,))
    service.refresh()
    return cur.rowcount > 0


# --------------------------------------------------------------------------- events

_PUBLIC_COLUMNS = ("id", "camera_id", "camera_name", "track_id", "entered_at", "last_in_zone_at", "left_at", "departure",
                   "status", "clip_status", "clip_error", "clip_frames", "expires_at", "entry_x", "entry_y",
                   "alert_seq", "alert_at")


def _public(row) -> dict:
    d = {k: row[k] for k in _PUBLIC_COLUMNS}
    d["clip_ready"] = row["clip_status"] == "ready" and bool(row["clip_path"])
    d["alert_seq"] = d["alert_seq"] or 1          # rows from before repeat alerts
    d["alert_at"] = d["alert_at"] or d["entered_at"]
    return d


def list_events(camera_id: int | None = None, limit: int = 50, after_id: int | None = None) -> list[dict]:
    q, params = "SELECT * FROM visitor_events WHERE expires_at > ?", [time.time()]
    if camera_id is not None:
        q += " AND camera_id = ?"
        params.append(camera_id)
    if after_id is not None:
        q += " AND id > ?"
        params.append(after_id)
    q += " ORDER BY COALESCE(alert_at, entered_at) DESC, id DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        return [_public(r) for r in conn.execute(q, params).fetchall()]


def get_event(event_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM visitor_events WHERE id = ? AND expires_at > ?", (event_id, time.time())).fetchone()
    return None if row is None else _public(row)


def clip_file(event_id: int) -> tuple[Path | None, str]:
    """(path, reason). path is None when the clip can't be served; reason says
    why: missing | expired | pending | failed | file_missing."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM visitor_events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return None, "missing"
    if row["expires_at"] <= time.time():
        return None, "expired"
    if row["clip_status"] == "pending":
        return None, "pending"
    if row["clip_status"] == "failed" or not row["clip_path"]:
        return None, "failed"
    p = (CLIPS_DIR.parent / row["clip_path"]).resolve()
    try:
        p.relative_to(CLIPS_DIR.resolve())
    except ValueError:
        return None, "missing"
    return (p, "ok") if p.is_file() else (None, "file_missing")


def delete_event(event_id: int) -> bool:
    """Manual removal: the clip file and the event row."""
    with _conn() as conn:
        row = conn.execute("SELECT clip_path FROM visitor_events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM visitor_events WHERE id = ?", (event_id,))
        conn.execute("DELETE FROM visitor_report_log WHERE event_id = ?", (event_id,))
    _remove_file(row["clip_path"])
    return True


def _remove_file(rel: str | None) -> None:
    if not rel:
        return
    p = (CLIPS_DIR.parent / rel).resolve()
    try:
        p.relative_to(CLIPS_DIR.resolve())
    except ValueError:
        return  # never delete outside the clips folder
    try:
        p.unlink(missing_ok=True)
    except OSError:
        log.exception("could not delete clip %s", p)


def purge_expired(now: float | None = None) -> dict:
    """Deletes every expired event (file + row), then clip files that no event
    points at any more (an interrupted write, a crash). Returns counts."""
    now = now if now is not None else time.time()
    with _conn() as conn:
        rows = conn.execute("SELECT id, clip_path FROM visitor_events WHERE expires_at <= ?", (now,)).fetchall()
        conn.executemany("DELETE FROM visitor_events WHERE id = ?", [(r["id"],) for r in rows])
    for r in rows:
        _remove_file(r["clip_path"])
    orphans = 0
    if CLIPS_DIR.is_dir():
        with _conn() as conn:
            known = {Path(r[0]).name for r in conn.execute("SELECT clip_path FROM visitor_events WHERE clip_path IS NOT NULL")}
        for f in CLIPS_DIR.rglob("*"):
            if f.is_file() and f.name not in known and now - f.stat().st_mtime > 3600:
                try:
                    f.unlink()
                    orphans += 1
                except OSError:
                    log.exception("could not delete orphan clip file %s", f)
    if rows or orphans:
        log.info("visitors: purged %d expired event(s) and %d orphan file(s)", len(rows), orphans)
    return {"expired_events": len(rows), "orphan_files": orphans}


def _clips_disk_mb() -> float:
    if not CLIPS_DIR.is_dir():
        return 0.0
    return sum(f.stat().st_size for f in CLIPS_DIR.rglob("*") if f.is_file()) / 1e6


def _iou(a, b) -> float:
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class _Track:
    __slots__ = ("inside_looks", "visit_key", "bbox")

    def __init__(self):
        self.inside_looks = 0
        self.visit_key: str | None = None
        self.bbox = None


class _Visit:
    """One continuous stay of one person in the area (in memory only).

    kind: pending  - not alerted yet, not recognised as an employee
          visitor  - alerted at least once
          employee - recognised as an enrolled person: never alerted, not counted
    """
    __slots__ = ("key", "track_id", "bbox", "entered_at", "last_in", "stable_since", "last_written",
                 "kind", "employee_id", "alert_ids", "next_due")

    def __init__(self, key, track_id, bbox, ts):
        self.key, self.track_id, self.bbox = key, track_id, bbox
        self.entered_at = self.last_in = self.stable_since = self.last_written = ts
        self.kind = "pending"
        self.employee_id: str | None = None
        self.alert_ids: list[int] = []
        self.next_due = ts  # the first alert is due at entry (after the detection gap)


class _KeepAliveSink:
    def put_nowait(self, item):
        pass


class VisitorService:
    def __init__(self):
        self._zones: dict[int, list] = {}
        self._tracks: dict[int, dict[int, _Track]] = {}
        self._open: dict[int, dict[str, _Visit]] = {}   # camera -> {visit key: visit}
        self._visit_seq = itertools.count(1)
        self._state_lock = threading.RLock()
        self._executors: dict[int, concurrent.futures.ThreadPoolExecutor] = {}
        self._futures: dict[int, concurrent.futures.Future] = {}
        self._health: dict[int, resilience.FeatureHealth] = {}
        self._sinks: dict[int, _KeepAliveSink] = {}
        # Rolling frame buffers (JPEG bytes), one per camera that has an area.
        self._buffers: dict[int, collections.deque] = {}
        self._buffer_lock = threading.Lock()
        self._last_tap: dict[int, float] = {}
        self._tap_executors: dict[int, concurrent.futures.ThreadPoolExecutor] = {}
        self._tap_futures: dict[int, concurrent.futures.Future] = {}
        self._clip_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="visitor-clip")
        self._started = False

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.refresh()
        self._close_orphaned_open_events()
        for target, name in ((self._housekeeping_loop, "visitors-housekeeping"), (self._purge_loop, "visitors-purge")):
            threading.Thread(target=target, daemon=True, name=name).start()

    def refresh(self) -> None:
        """Reload the saved areas (after a save/clear, and at start-up)."""
        zones = {z["camera_id"]: z["polygon"] for z in list_zones() if z["enabled"]}
        with self._state_lock:
            self._zones = zones
            for cid in [c for c in self._tracks if c not in zones]:
                self._tracks.pop(cid, None)
        with self._buffer_lock:
            for cid in [c for c in self._buffers if c not in zones]:
                self._buffers.pop(cid, None)
        try:
            self.sync()
        except Exception:
            log.exception("visitors: keep-alive sync failed")

    def _close_orphaned_open_events(self) -> None:
        """After a restart nothing is in memory: visits left open are closed at
        the last moment they were seen inside (departure 'assumed')."""
        now = time.time()
        with _conn() as conn:
            rows = conn.execute("SELECT id, last_in_zone_at, expires_at FROM visitor_events WHERE status = 'inside'").fetchall()
            for r in rows:
                left = r["last_in_zone_at"]
                conn.execute("UPDATE visitor_events SET status = 'left', left_at = ?, departure = 'assumed', expires_at = ? WHERE id = ?",
                             (left, min(r["expires_at"], left + RETENTION_AFTER_DEPARTURE_HOURS * 3600), r["id"]))
                conn.execute("UPDATE visitor_report_log SET left_at = ?, departure = 'assumed' WHERE event_id = ? AND left_at IS NULL",
                             (left, r["id"]))
        if rows:
            log.info("visitors: closed %d visit(s) left open by a restart", len(rows))

    def sync(self) -> None:
        """Keep cameras that have an area streaming (no viewer needed)."""
        from app import camera_stream

        on = analytics_settings.enabled(FEATURE)
        wanted = set(self._zones) if on else set()
        wanted &= {c["id"] for c in camera_db.list_cameras() if camera_db.is_streamable(c)}
        for cid in wanted - self._sinks.keys():
            sink = _KeepAliveSink()
            camera_stream.get_stream(cid).subscribe(sink, is_collector=True, wants_frames=False)
            self._sinks[cid] = sink
        for cid in self._sinks.keys() - wanted:
            camera_stream.get_stream(cid).unsubscribe(self._sinks.pop(cid))

    def _housekeeping_loop(self) -> None:
        while True:
            try:
                self.sync()
                self._close_departed_all()
            except Exception:
                log.exception("visitors: housekeeping failed")
            if lifecycle.wait(HOUSEKEEPING_SECONDS):
                return

    def _purge_loop(self) -> None:
        while True:
            try:
                purge_expired()
            except Exception:
                log.exception("visitors: purge failed")
            if lifecycle.wait(PURGE_INTERVAL_SECONDS):
                return

    # ---- person_detection consumer -------------------------------------------

    def health(self, camera_id: int) -> resilience.FeatureHealth:
        h = self._health.get(camera_id)
        if h is None:
            h = self._health.setdefault(camera_id, resilience.health_for("visitors", camera_id))
        return h

    def wants(self, camera_id: int) -> float:
        if camera_id not in self._zones or not analytics_settings.enabled(FEATURE) or not self.health(camera_id).allow():
            return 0.0
        return DETECT_FPS

    def deliver(self, camera_id: int, frame: np.ndarray, people: list[dict], ts: float) -> None:
        fut = self._futures.get(camera_id)
        if fut is not None and not fut.done():
            return  # still on the previous look: skip, never queue
        polygon = self._zones.get(camera_id)
        if polygon is None:
            return
        h, w = frame.shape[:2]
        ex = self._executors.setdefault(camera_id, concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"visitors-{camera_id}"))
        self._futures[camera_id] = ex.submit(self._process, camera_id, polygon, w, h, people, ts)

    def _process(self, camera_id: int, polygon: list, w: int, h: int, people: list[dict], ts: float) -> None:
        try:
            self.process(camera_id, polygon, w, h, people, ts)
        except Exception as e:
            self.health(camera_id).failure(e)
        else:
            self.health(camera_id).success()

    def process(self, camera_id: int, polygon: list, w: int, h: int, people: list[dict], ts: float) -> None:
        """One look at one camera. Pure logic (no frames), so it can be tested directly."""
        inside: dict[int, list] = {}
        for p in people:
            tid = p.get("track_id")
            if tid is None or p["confidence"] < PERSON_CONF:
                continue
            b = p["bbox"]
            if in_polygon((b[0] + b[2]) / 2 / w, b[3] / h, polygon):
                inside[tid] = b
        with self._state_lock:
            tracks = self._tracks.setdefault(camera_id, {})
            visits = self._open.setdefault(camera_id, {})
            for tid, bbox in inside.items():
                t = tracks.setdefault(tid, _Track())
                t.inside_looks += 1
                t.bbox = bbox
                v = visits.get(t.visit_key) if t.visit_key else None
                if v is None:
                    if t.inside_looks < ENTRY_CONFIRM_LOOKS:
                        continue
                    v = self._reattach(camera_id, tid, bbox, ts, inside) or self._open_visit(camera_id, tid, bbox, ts)
                    t.visit_key = v.key
                self._seen(v, tid, bbox, ts)
                if EXCLUDE_EMPLOYEES and v.kind != "employee":
                    employee = self.identify(camera_id, bbox, w, h)
                    if employee:
                        self._mark_employee(camera_id, v, employee)
                self._maybe_alert(camera_id, v, ts, w, h)
            for tid, t in list(tracks.items()):
                if tid not in inside:
                    t.inside_looks = 0
                    if t.visit_key is None or t.visit_key not in visits:
                        tracks.pop(tid, None)  # a track that is not (or no longer) a visit
            self._close_departed(camera_id, ts)

    # ---- visits ----------------------------------------------------------------

    def _open_visit(self, camera_id: int, tid: int, bbox, ts: float) -> _Visit:
        v = _Visit(f"{camera_id}-{int(ts * 1000)}-{next(self._visit_seq)}", tid, bbox, ts)
        self._open.setdefault(camera_id, {})[v.key] = v
        log.info("visitors: visit %s - person (track %s) in the area on camera %s", v.key, tid, camera_id)
        return v

    def _reattach(self, camera_id: int, tid: int, bbox, ts: float, inside_now: dict) -> _Visit | None:
        """The tracker gave someone already in a visit a new id (after an
        occlusion, or a moment out of sight): continue that visit, its alert
        timer and its identity, instead of starting another one. The best
        overlapping visit whose own track is not in the area right now.

        An employee visit is only taken over by an immediate re-label
        (ID_SWITCH_SECONDS): a different person sitting down on the same seat a
        bit later must not inherit the employee exclusion."""
        best, best_iou = None, ID_SWITCH_IOU
        for v in self._open.get(camera_id, {}).values():
            if v.track_id == tid or v.track_id in inside_now:
                continue
            limit = ID_SWITCH_SECONDS if v.kind == "employee" else ABSENCE_GRACE_SECONDS
            if ts - v.last_in > limit:
                continue
            iou = _iou(v.bbox, bbox)
            if iou >= best_iou:
                best, best_iou = v, iou
        if best is None:
            return None
        old = self._tracks.get(camera_id, {}).get(best.track_id)
        if old is not None:
            old.visit_key = None
        log.info("visitors: visit %s continues on camera %s (track %s -> %s)", best.key, camera_id, best.track_id, tid)
        best.track_id = tid
        return best

    def _seen(self, v: _Visit, tid: int, bbox, ts: float) -> None:
        # A loss longer than PRESENCE_BREAK_SECONDS restarts the detection-gap
        # check only. It never resets the visit or its repeat-alert timer.
        if ts - v.last_in > PRESENCE_BREAK_SECONDS:
            v.stable_since = ts
        v.track_id, v.bbox, v.last_in = tid, bbox, ts
        if v.alert_ids and ts - v.last_written >= LAST_IN_WRITE_SECONDS:
            v.last_written = ts
            self._db_touch(v.key, ts)

    def _maybe_alert(self, camera_id: int, v: _Visit, ts: float, w: int, h: int) -> None:
        """At most one alert per due time: due at entry, then every
        REPEAT_ALERT_MINUTES, each fired only after DETECTION_GAP_SECONDS of
        unbroken presence since it became due."""
        if v.kind == "employee" or ts < v.next_due:
            return
        if ts - max(v.stable_since, v.next_due) < DETECTION_GAP_SECONDS:
            return
        self._alert(camera_id, v, ts, w, h)
        v.kind = "visitor"
        v.next_due = ts + REPEAT_ALERT_MINUTES * 60 if REPEAT_ALERT_MINUTES > 0 else float("inf")

    def _alert(self, camera_id: int, v: _Visit, ts: float, w: int, h: int) -> None:
        cam = camera_db.get_camera(camera_id) or {}
        bbox = v.bbox
        expires = ts + (MAX_PRESENCE_HOURS + RETENTION_AFTER_DEPARTURE_HOURS) * 3600  # fallback cap, tightened on departure
        seq = len(v.alert_ids) + 1
        with _conn() as conn:
            cur = conn.execute(
                "INSERT INTO visitor_events (camera_id, camera_name, track_id, entered_at, last_in_zone_at, status, clip_status, expires_at, created_at, "
                "entry_x, entry_y, visit_key, alert_seq, alert_at) VALUES (?, ?, ?, ?, ?, 'inside', 'pending', ?, ?, ?, ?, ?, ?, ?)",
                (camera_id, cam.get("name") or f"Camera {camera_id}", v.track_id, v.entered_at, ts, expires, time.time(),
                 round((bbox[0] + bbox[2]) / 2 / w, 4), round(bbox[3] / h, 4), v.key, seq, ts),
            )
            event_id = cur.lastrowid
            conn.execute(
                "INSERT OR REPLACE INTO visitor_report_log (event_id, camera_id, camera_name, visit_key, alert_seq, entered_at, alert_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event_id, camera_id, cam.get("name") or f"Camera {camera_id}", v.key, seq, v.entered_at, ts),
            )
        v.alert_ids.append(event_id)
        v.last_written = ts
        log.info("visitors: alert %s (#%d of visit %s) - unknown person in the area on camera %s", event_id, seq, v.key, camera_id)
        self._schedule_clip(event_id, camera_id, ts)

    def _mark_employee(self, camera_id: int, v: _Visit, employee_id: str) -> None:
        """Recognised as an enrolled person: no alerts for this visit, and an
        alert already raised for it (recognition came late) is taken back, with
        its clip, so employees never show as visitors."""
        v.kind, v.employee_id = "employee", employee_id
        if v.alert_ids:
            for eid in v.alert_ids:
                delete_event(eid)
            log.info("visitors: visit %s on camera %s is an employee; removed %d alert(s)", v.key, camera_id, len(v.alert_ids))
            v.alert_ids = []
        else:
            log.info("visitors: visit %s on camera %s is an employee (no alert)", v.key, camera_id)

    def identify(self, camera_id: int, bbox, w: int, h: int) -> str | None:
        """The enrolled person the face pipeline has identified at this box on
        this camera, or None (not identified yet, or face recognition is not
        running on the camera). Reads the pipeline's live tracks only: no
        inference here. Boxes are compared as fractions of their own frame."""
        from app import face_pipeline

        pipe = face_pipeline.get_existing_pipeline(camera_id)
        if pipe is None or not getattr(pipe, "frame_size", None):
            return None
        fw, fh = pipe.frame_size
        mine = [bbox[0] / w, bbox[1] / h, bbox[2] / w, bbox[3] / h]
        best, best_iou = None, EMPLOYEE_MATCH_IOU
        for d in pipe.get_live_detections():
            if not d.get("employee_id") or d.get("identity_source") not in EMPLOYEE_SOURCES:
                continue
            b = d["bbox"]
            iou = _iou(mine, [b[0] / fw, b[1] / fh, b[2] / fw, b[3] / fh])
            if iou >= best_iou:
                best, best_iou = str(d["employee_id"]), iou
        return best

    def exclusion_available(self, camera_id: int) -> bool:
        """Whether employees can be told apart on this camera right now."""
        if not EXCLUDE_EMPLOYEES:
            return False
        from app import face_pipeline

        return face_pipeline.get_existing_pipeline(camera_id) is not None

    def _db_touch(self, visit_key: str, ts: float) -> None:
        try:
            with _conn() as conn:
                conn.execute("UPDATE visitor_events SET last_in_zone_at = ? WHERE visit_key = ? AND status = 'inside'", (ts, visit_key))
        except sqlite3.Error:
            log.exception("visitors: could not update visit %s", visit_key)

    def _close_departed(self, camera_id: int, now: float) -> None:
        for v in list(self._open.get(camera_id, {}).values()):
            if now - v.last_in > ABSENCE_GRACE_SECONDS:
                self._leave(camera_id, v, v.last_in, "tracked")
            elif now - v.entered_at > MAX_PRESENCE_HOURS * 3600:
                self._leave(camera_id, v, now, "assumed")

    def _close_departed_all(self) -> None:
        now = time.time()
        with self._state_lock:
            for cid in list(self._open):
                self._close_departed(cid, now)

    def _leave(self, camera_id: int, v: _Visit, left_at: float, how: str) -> None:
        self._open.get(camera_id, {}).pop(v.key, None)
        t = self._tracks.get(camera_id, {}).get(v.track_id)
        if t is not None:
            t.visit_key = None
            t.inside_looks = 0
        log.info("visitors: visit %s left the area on camera %s (%s, %s)", v.key, camera_id, how, v.kind)
        if not v.alert_ids:
            return  # employees and people who never qualified leave nothing behind
        try:
            with _conn() as conn:
                conn.execute(
                    "UPDATE visitor_events SET status = 'left', left_at = ?, departure = ?, last_in_zone_at = ?, "
                    "expires_at = MIN(expires_at, ?) WHERE visit_key = ? AND status = 'inside'",
                    (left_at, how, left_at, left_at + RETENTION_AFTER_DEPARTURE_HOURS * 3600, v.key),
                )
                conn.execute("UPDATE visitor_report_log SET left_at = ?, departure = ? WHERE visit_key = ? AND left_at IS NULL",
                             (left_at, how, v.key))
        except sqlite3.Error:
            log.exception("visitors: could not close visit %s", v.key)

    # ---- rolling buffer + clips ----------------------------------------------

    def tap(self, camera_id: int, frame: np.ndarray) -> None:
        """Called by camera_stream for every frame. Cheap unless this camera has an
        area: then (at most BUFFER_FPS times a second, on a worker, skipped when
        busy) a small JPEG goes into the rolling buffer."""
        if camera_id not in self._zones or not analytics_settings.enabled(FEATURE):
            return
        now = time.time()
        if now - self._last_tap.get(camera_id, 0.0) < 1.0 / BUFFER_FPS:
            return
        fut = self._tap_futures.get(camera_id)
        if fut is not None and not fut.done():
            return
        self._last_tap[camera_id] = now
        ex = self._tap_executors.setdefault(camera_id, concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"visitor-tap-{camera_id}"))
        self._tap_futures[camera_id] = ex.submit(self._buffer_frame, camera_id, frame, now)

    def _buffer_frame(self, camera_id: int, frame: np.ndarray, ts: float) -> None:
        try:
            h, w = frame.shape[:2]
            img = frame if w <= BUFFER_WIDTH else cv2.resize(frame, (BUFFER_WIDTH, round(h * BUFFER_WIDTH / w)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok:
                return
            with self._buffer_lock:
                dq = self._buffers.get(camera_id)
                if dq is None:
                    dq = self._buffers[camera_id] = collections.deque(maxlen=int(BUFFER_SECONDS * BUFFER_FPS) + 4)
                dq.append((ts, buf.tobytes()))
        except Exception:
            log.exception("visitors: could not buffer a frame from camera %s", camera_id)

    def _schedule_clip(self, event_id: int, camera_id: int, t0: float) -> None:
        # Wait for the 'after' half, then hand the work to the single encoder thread.
        timer = threading.Timer(POST_SECONDS + 0.8, lambda: self._clip_executor.submit(self._write_clip, event_id, camera_id, t0))
        timer.daemon = True
        timer.start()

    def _set_clip(self, event_id: int, status: str, path: str | None = None, error: str | None = None, frames: int | None = None) -> bool:
        with _conn() as conn:
            cur = conn.execute("UPDATE visitor_events SET clip_status = ?, clip_path = ?, clip_error = ?, clip_frames = ? WHERE id = ?",
                               (status, path, error, frames, event_id))
            return cur.rowcount > 0

    def _write_clip(self, event_id: int, camera_id: int, t0: float) -> None:
        try:
            with self._buffer_lock:
                frames = [(ts, jpg) for ts, jpg in self._buffers.get(camera_id, ()) if t0 - PRE_SECONDS <= ts <= t0 + POST_SECONDS]
            if not frames:
                self._set_clip(event_id, "failed", error="No video was available from this camera (it may have been offline).")
                return
            if _clips_disk_mb() > MAX_CLIPS_DISK_MB:
                self._set_clip(event_id, "failed", error="Clip storage limit reached.")
                return
            CLIPS_DIR.joinpath(str(camera_id)).mkdir(parents=True, exist_ok=True)
            final = CLIPS_DIR / str(camera_id) / f"event_{event_id}.mp4"
            part = final.with_suffix(".part.mp4")
            span = max(0.5, frames[-1][0] - frames[0][0])
            fps = max(2, min(12, round(len(frames) / span)))
            _encode_mp4(part, [jpg for _ts, jpg in frames], fps)
            os.replace(part, final)
            if not self._set_clip(event_id, "ready", path=final.relative_to(CLIPS_DIR.parent).as_posix(), frames=len(frames)):
                final.unlink(missing_ok=True)  # the event was deleted while we were encoding
        except Exception as e:
            log.exception("visitors: could not write the clip for event %s", event_id)
            try:
                self._set_clip(event_id, "failed", error=f"Recording failed ({type(e).__name__}).")
            except Exception:
                pass

    # ---- reporting ------------------------------------------------------------

    def camera_status(self, camera_id: int) -> dict:
        from app import camera_stream

        stream = camera_stream.get_stream(camera_id)
        online = stream.state == camera_stream.S_RUNNING and stream.last_frame_at is not None and time.time() - stream.last_frame_at < 15
        zone = get_zone(camera_id)
        active = bool(zone and zone["enabled"] and analytics_settings.enabled(FEATURE))
        kinds = [v.kind for v in list(self._open.get(camera_id, {}).values())]
        return {"online": online, "stream_state": stream.state, "zone": zone, "detection_active": active,
                # visitors only: recognised employees are never counted as visitors
                "inside_now": kinds.count("visitor"),
                "employees_now": kinds.count("employee"),
                "unverified_now": kinds.count("pending"),
                "employee_exclusion": self.exclusion_available(camera_id)}


def _encode_mp4(path: Path, jpegs: list[bytes], fps: int) -> None:
    """H.264 (yuv420p) MP4 with the index at the front, so browsers play and seek it."""
    import av

    first = cv2.imdecode(np.frombuffer(jpegs[0], np.uint8), cv2.IMREAD_COLOR)
    h, w = first.shape[:2]
    w, h = w - w % 2, h - h % 2
    container = av.open(str(path), "w", options={"movflags": "+faststart"})
    try:
        stream = container.add_stream("libx264", rate=fps)
        stream.width, stream.height, stream.pix_fmt = w, h, "yuv420p"
        stream.options = {"crf": "28", "preset": "veryfast"}
        for jpg in jpegs:
            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            frame = av.VideoFrame.from_ndarray(img[:h, :w], format="bgr24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()


service = VisitorService()
person_detection.service.register("visitors", service.wants, service.deliver)
