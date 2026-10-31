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
3D area        The polygon is the FLOOR of the area. An optional height (fraction
               of the frame height, 0 = flat 2D area, which is what areas saved
               before this existed load as) turns it into a box drawn in
               perspective on the live video. The height is for drawing only: the
               zone rule above uses the floor polygon, because feet stand on the
               floor and a box's height says nothing more about who is inside.
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

import concurrent.futures
import datetime
import itertools
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

import numpy as np

from app import analytics_settings, camera_db, clips, config, db, lifecycle, person_detection, resilience
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
# Where face recognition runs on the camera, the FIRST alert of a visit also
# waits until the person has been in the area this long, so their face can be
# identified before anyone is called a visitor (a later identification still
# takes a wrong alert back).
VERIFY_SECONDS = float(os.environ.get("VISITOR_VERIFY_SECONDS", "30"))
# How often the faces inside unverified visitors' boxes are identified, per camera.
FACE_CHECK_SECONDS = float(os.environ.get("VISITOR_FACE_CHECK_SECONDS", "1"))
FACE_CHECKS_PER_CYCLE = 3
RECOGNITION_STARTUP_WAIT_SECONDS = float(os.environ.get("VISITOR_RECOGNITION_STARTUP_WAIT_SECONDS", "180"))
ID_SWITCH_SECONDS = 3.0   # a new track id this soon after, overlapping, is the same person
ID_SWITCH_IOU = 0.3

# The rolling buffer and clip encoder are shared with Footfall (clips.py).
PRE_SECONDS = clips.PRE_SECONDS
POST_SECONDS = clips.POST_SECONDS
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
                   polygon TEXT NOT NULL,            -- JSON [[x, y], ...] fractions of the frame (the floor)
                   enabled INTEGER NOT NULL DEFAULT 1,
                   updated_at REAL NOT NULL,
                   height REAL NOT NULL DEFAULT 0)   -- 3D box height, fraction of the frame height; 0 = flat"""
        )
        try:  # areas saved before 3D areas existed stay flat (height 0)
            conn.execute("ALTER TABLE visitor_zones ADD COLUMN height REAL NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
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
        try:  # tables created before last_seen existed
            conn.execute("ALTER TABLE visitor_report_log ADD COLUMN last_seen REAL")
        except sqlite3.OperationalError:
            pass
        # EMPLOYEES seen in a visitor area, kept apart from visitor records: one
        # row per stay of a recognised employee. Never an alert, never a clip.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS visitor_area_employee_log (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   camera_id INTEGER NOT NULL,
                   camera_name TEXT,
                   employee_id TEXT NOT NULL,
                   entered_at REAL NOT NULL,
                   last_seen REAL NOT NULL,
                   left_at REAL)"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor_area_employee_log_ts ON visitor_area_employee_log (entered_at)")
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


def _check_height(height) -> float:
    if not isinstance(height, (int, float)) or not (0 <= height <= 1):
        raise ValueError("Height must be between 0 and 1 (fraction of the picture height)")
    return round(float(height), 5)


def _zone_dict(row) -> dict:
    return {"camera_id": row["camera_id"], "polygon": json.loads(row["polygon"]), "height": row["height"] or 0.0,
            "enabled": bool(row["enabled"]), "updated_at": row["updated_at"]}


def get_zone(camera_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM visitor_zones WHERE camera_id = ?", (camera_id,)).fetchone()
    return None if row is None else _zone_dict(row)


def list_zones() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM visitor_zones").fetchall()
    return [_zone_dict(r) for r in rows]


def save_zone(camera_id: int, polygon, enabled: bool = True, height: float = 0.0) -> dict:
    polygon = _check_polygon(polygon)
    height = _check_height(height)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO visitor_zones (camera_id, polygon, enabled, updated_at, height) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(camera_id) DO UPDATE SET polygon = excluded.polygon, enabled = excluded.enabled, "
            "updated_at = excluded.updated_at, height = excluded.height",
            (camera_id, json.dumps(polygon), 1 if enabled else 0, time.time(), height),
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


def list_visits(camera_id: int | None = None, limit: int = 100, since: float | None = None,
                until: float | None = None) -> list[dict]:
    """Visitor history: one row per VISIT (repeat alerts of the same stay are
    grouped, never counted as extra visitors), newest first. From the permanent
    log, so it outlives the clips; latest_event_id is set while that alert (and
    its clip) still exists."""
    q = ("SELECT COALESCE(l.visit_key, 'event-' || l.event_id) AS visit, MIN(l.camera_id) AS camera_id, "
         "MIN(l.camera_name) AS camera_name, MIN(l.entered_at) AS arrived_at, "
         "MAX(COALESCE(l.last_seen, l.left_at, l.alert_at)) AS last_seen, MAX(l.left_at) AS left_at, "
         "SUM(CASE WHEN l.left_at IS NULL THEN 1 ELSE 0 END) AS open_rows, COUNT(*) AS alerts, "
         "MIN(l.alert_at) AS first_alert_at, MAX(l.alert_at) AS last_alert_at, MAX(e.id) AS latest_event_id "
         "FROM visitor_report_log l LEFT JOIN visitor_events e ON e.id = l.event_id AND e.expires_at > ? WHERE 1 = 1")
    params: list = [time.time()]
    if camera_id is not None:
        q += " AND l.camera_id = ?"
        params.append(camera_id)
    if since is not None:
        q += " AND l.alert_at >= ?"
        params.append(since)
    if until is not None:
        q += " AND l.alert_at < ?"
        params.append(until)
    q += " GROUP BY visit ORDER BY arrived_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(q, params).fetchall()
    out = []
    for r in rows:
        inside = r["open_rows"] > 0
        out.append({
            "visit": r["visit"], "camera_id": r["camera_id"], "camera_name": r["camera_name"],
            "arrived_at": r["arrived_at"], "last_seen": r["last_seen"],
            "left_at": None if inside else r["left_at"], "status": "inside" if inside else "left",
            "duration_seconds": max(0.0, (r["last_seen"] or r["arrived_at"]) - r["arrived_at"]),
            "alerts": r["alerts"], "first_alert_at": r["first_alert_at"], "last_alert_at": r["last_alert_at"],
            "latest_event_id": r["latest_event_id"],
        })
    return out


def kpis(now: float | None = None, day: str | None = None) -> dict:
    """KPI cards for one local day (default today): visitors (visits, never
    alerts), their average stay in the area, stays over the repeat-alert
    interval, still there, alerts raised and arrivals per hour."""
    now = now if now is not None else time.time()
    start_dt = (datetime.datetime.strptime(day, "%Y-%m-%d") if day
                else datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0))
    day_start = start_dt.timestamp()
    day_end = (start_dt + datetime.timedelta(days=1)).timestamp()
    visits = list_visits(limit=10_000, since=day_start, until=day_end)
    hourly: dict[int, int] = {}
    for v in visits:
        h = datetime.datetime.fromtimestamp(v["arrived_at"]).hour
        hourly[h] = hourly.get(h, 0) + 1
    long_after = (REPEAT_ALERT_MINUTES if REPEAT_ALERT_MINUTES > 0 else 30) * 60
    stays = [v["duration_seconds"] for v in visits]
    return {
        "visitors_today": len(visits),
        "average_stay_seconds": round(sum(stays) / len(stays)) if stays else 0,
        "long_stays": sum(1 for s in stays if s >= long_after),
        "long_stay_minutes": long_after / 60,
        "waiting_now": sum(1 for v in visits if v["status"] == "inside"),
        "employees_seen_today": len({r["employee_id"] for r in list_employee_visits(limit=10_000)
                                     if day_start <= r["arrived_at"] < day_end}),
        "alerts": sum(v["alerts"] for v in visits),
        "hourly": [{"hour": h, "visitors": hourly[h]} for h in sorted(hourly)],
        "peak_hour": max(hourly, key=hourly.get) if hourly else None,
    }


def list_employee_visits(camera_id: int | None = None, limit: int = 100) -> list[dict]:
    """Recognised employees seen in a visitor area (kept apart from visitors)."""
    q, params = "SELECT * FROM visitor_area_employee_log WHERE 1 = 1", []
    if camera_id is not None:
        q += " AND camera_id = ?"
        params.append(camera_id)
    q += " ORDER BY entered_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [{"id": r["id"], "camera_id": r["camera_id"], "camera_name": r["camera_name"], "employee_id": r["employee_id"],
             "arrived_at": r["entered_at"], "last_seen": r["last_seen"], "left_at": r["left_at"],
             "status": "inside" if r["left_at"] is None else "left"} for r in rows]


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
                 "kind", "employee_id", "alert_ids", "next_due", "employee_log_id", "frame_wh")

    def __init__(self, key, track_id, bbox, ts):
        self.key, self.track_id, self.bbox = key, track_id, bbox
        self.entered_at = self.last_in = self.stable_since = self.last_written = ts
        self.kind = "pending"
        self.employee_id: str | None = None
        self.alert_ids: list[int] = []
        self.next_due = ts  # the first alert is due at entry (after the detection gap)
        self.employee_log_id: int | None = None
        self.frame_wh = (1, 1)  # size of the frame bbox is in (set on every look)


class _KeepAliveSink:
    def put_nowait(self, item):
        pass


class VisitorService:
    def __init__(self):
        self._zones: dict[int, list] = {}
        self._tracks: dict[int, dict[int, _Track]] = {}
        self._open: dict[int, dict[str, _Visit]] = {}   # camera -> {visit key: visit}
        self._visit_seq = itertools.count(1)
        self._last_face_check: dict[int, float] = {}
        self._recognition_since: dict[int, float] = {}  # camera -> first look with recognition running
        self._first_look: dict[int, float] = {}          # camera -> first look since start-up
        self._state_lock = threading.RLock()
        self._executors: dict[int, concurrent.futures.ThreadPoolExecutor] = {}
        self._futures: dict[int, concurrent.futures.Future] = {}
        self._health: dict[int, resilience.FeatureHealth] = {}
        self._sinks: dict[int, _KeepAliveSink] = {}
        # Rolling frame buffers (JPEG bytes), one per camera that has an area.
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
        clips.recorder.want(FEATURE, wanted)  # rolling clip buffer for the cameras with an area
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
            self._note_recognition(camera_id, ts)
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
                v.frame_wh = (w, h)
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
        if (v.alert_ids or v.employee_log_id) and ts - v.last_written >= LAST_IN_WRITE_SECONDS:
            v.last_written = ts
            self._db_touch(v, ts)

    def _maybe_alert(self, camera_id: int, v: _Visit, ts: float, w: int, h: int) -> None:
        """At most one alert per due time: due at entry, then every
        REPEAT_ALERT_MINUTES, each fired only after DETECTION_GAP_SECONDS of
        unbroken presence since it became due."""
        if v.kind == "employee" or ts < v.next_due:
            return
        if ts - max(v.stable_since, v.next_due) < DETECTION_GAP_SECONDS:
            return
        if not v.alert_ids and not self._identification_done(camera_id, v, ts):
            return  # still giving face recognition its chance to name an employee
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
                "INSERT OR REPLACE INTO visitor_report_log (event_id, camera_id, camera_name, visit_key, alert_seq, entered_at, alert_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (event_id, camera_id, cam.get("name") or f"Camera {camera_id}", v.key, seq, v.entered_at, ts, ts),
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
        # Kept as an EMPLOYEE record, apart from visitor records.
        cam = camera_db.get_camera(camera_id) or {}
        try:
            with _conn() as conn:
                cur = conn.execute(
                    "INSERT INTO visitor_area_employee_log (camera_id, camera_name, employee_id, entered_at, last_seen) VALUES (?, ?, ?, ?, ?)",
                    (camera_id, cam.get("name") or f"Camera {camera_id}", employee_id, v.entered_at, v.last_in),
                )
                v.employee_log_id = cur.lastrowid
            v.last_written = v.last_in
        except sqlite3.Error:
            log.exception("visitors: could not record employee visit %s", v.key)

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

    def _identification_done(self, camera_id: int, v: _Visit, ts: float) -> bool:
        """Whether face recognition has had its chance with this person: they
        have been in the area for VERIFY_SECONDS while recognition was RUNNING
        on this camera. Recognition still starting (the models load for a
        minute or two after a restart) holds the first alert, for at most
        RECOGNITION_STARTUP_WAIT_SECONDS; a camera without face recognition
        does not wait."""
        if not EXCLUDE_EMPLOYEES:
            return True
        since = self._recognition_since.get(camera_id)
        if since is not None:
            return ts - max(v.entered_at, since) >= VERIFY_SECONDS
        if not self._recognition_expected(camera_id):
            return True
        return ts - self._first_look.get(camera_id, ts) >= RECOGNITION_STARTUP_WAIT_SECONDS

    def _note_recognition(self, camera_id: int, ts: float) -> None:
        """Called on every look: when this camera was first looked at, and the
        first look at which face recognition was running on it."""
        self._first_look.setdefault(camera_id, ts)
        if EXCLUDE_EMPLOYEES and camera_id not in self._recognition_since and self.exclusion_available(camera_id):
            self._recognition_since[camera_id] = ts

    @staticmethod
    def _recognition_expected(camera_id: int) -> bool:
        """Face recognition is switched on for this camera (so its pipeline is
        coming), as camera_stream decides it."""
        cam = camera_db.get_camera(camera_id)
        return bool(cam and cam.get("attendance_tracking", 1) and analytics_settings.enabled("face_recognition"))

    def wants_faces(self, camera_id: int) -> bool:
        """Whether the face pipeline should hand this camera's faces over: only
        while someone in its visitor area is not yet known to be an employee."""
        if not EXCLUDE_EMPLOYEES or camera_id not in self._zones or not analytics_settings.enabled(FEATURE):
            return False
        if time.time() - self._last_face_check.get(camera_id, 0.0) < FACE_CHECK_SECONDS:
            return False
        return any(v.kind != "employee" for v in list(self._open.get(camera_id, {}).values()))

    def observe_faces(self, camera_id: int, frame_shape, face_boxes: list, identify) -> None:
        """Called from the face pipeline's face worker with this frame's face
        boxes (pixels). Each face whose centre is in the head part of a not-yet-
        verified visitor's body box is identified (identify(bbox) -> (employee
        id | None, confidence), the live-overlay recognition rules); a match
        makes that visit an employee visit. Identification runs outside the
        state lock: it is the slow part."""
        self._last_face_check[camera_id] = time.time()
        fh, fw = frame_shape[:2]
        with self._state_lock:
            todo = []
            for v in self._open.get(camera_id, {}).values():
                if v.kind == "employee":
                    continue
                vw, vh = v.frame_wh
                x1, y1, x2, y2 = v.bbox[0] / vw, v.bbox[1] / vh, v.bbox[2] / vw, v.bbox[3] / vh
                head_bottom = y1 + 0.6 * (y2 - y1)
                for fb in face_boxes:
                    cx, cy = (fb[0] + fb[2]) / 2 / fw, (fb[1] + fb[3]) / 2 / fh
                    if x1 <= cx <= x2 and y1 <= cy <= head_bottom:
                        todo.append((v.key, fb))
                        break
        for key, fb in todo[:FACE_CHECKS_PER_CYCLE]:
            try:
                employee, _conf = identify(fb)
            except Exception:
                log.exception("visitors: face identification failed on camera %s", camera_id)
                continue
            if not employee:
                continue
            with self._state_lock:
                v = self._open.get(camera_id, {}).get(key)
                if v is not None and v.kind != "employee":
                    self._mark_employee(camera_id, v, str(employee))

    def exclusion_available(self, camera_id: int) -> bool:
        """Whether employees can be told apart on this camera right now."""
        if not EXCLUDE_EMPLOYEES:
            return False
        from app import face_pipeline

        pipe = face_pipeline.get_existing_pipeline(camera_id)
        # Running = its models are loaded AND it has finished a face pass on
        # this camera (right after a restart the pipeline exists well before that).
        return (pipe is not None and getattr(type(pipe), "_arcface", None) is not None
                and pipe.__dict__.get("_faces_latest") is not None)

    def _db_touch(self, v: _Visit, ts: float) -> None:
        """Last-seen time of an ongoing visit (rate-limited by the caller)."""
        try:
            with _conn() as conn:
                if v.employee_log_id:
                    conn.execute("UPDATE visitor_area_employee_log SET last_seen = ? WHERE id = ?", (ts, v.employee_log_id))
                if v.alert_ids:
                    conn.execute("UPDATE visitor_events SET last_in_zone_at = ? WHERE visit_key = ? AND status = 'inside'", (ts, v.key))
                    conn.execute("UPDATE visitor_report_log SET last_seen = ? WHERE visit_key = ? AND left_at IS NULL", (ts, v.key))
        except sqlite3.Error:
            log.exception("visitors: could not update visit %s", v.key)

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
        if v.employee_log_id:
            try:
                with _conn() as conn:
                    conn.execute("UPDATE visitor_area_employee_log SET left_at = ?, last_seen = MAX(last_seen, ?) WHERE id = ?",
                                 (left_at, min(left_at, v.last_in), v.employee_log_id))
            except sqlite3.Error:
                log.exception("visitors: could not close employee visit %s", v.key)
        if not v.alert_ids:
            return  # people who never qualified as visitors leave no visitor record
        try:
            with _conn() as conn:
                conn.execute(
                    "UPDATE visitor_events SET status = 'left', left_at = ?, departure = ?, last_in_zone_at = ?, "
                    "expires_at = MIN(expires_at, ?) WHERE visit_key = ? AND status = 'inside'",
                    (left_at, how, left_at, left_at + RETENTION_AFTER_DEPARTURE_HOURS * 3600, v.key),
                )
                conn.execute("UPDATE visitor_report_log SET left_at = ?, departure = ?, last_seen = ? WHERE visit_key = ? AND left_at IS NULL",
                             (left_at, how, min(left_at, v.last_in), v.key))
        except sqlite3.Error:
            log.exception("visitors: could not close visit %s", v.key)

    # ---- clips (shared recorder, clips.py) -------------------------------------

    def tap(self, camera_id: int, frame: np.ndarray) -> None:
        """Kept for callers of the old API: the shared recorder does the buffering."""
        clips.recorder.tap(camera_id, frame)

    def _schedule_clip(self, event_id: int, camera_id: int, t0: float) -> None:
        final = CLIPS_DIR / str(camera_id) / f"event_{event_id}.mp4"

        def done(status, frames=None, error=None):
            if status == "ready":
                if not self._set_clip(event_id, "ready", path=final.relative_to(CLIPS_DIR.parent).as_posix(), frames=frames):
                    final.unlink(missing_ok=True)  # the event was deleted while we were encoding
            else:
                self._set_clip(event_id, "failed", error=error)

        clips.recorder.schedule(camera_id, t0, final, done, max_dir_mb=MAX_CLIPS_DISK_MB)

    def _set_clip(self, event_id: int, status: str, path: str | None = None, error: str | None = None, frames: int | None = None) -> bool:
        with _conn() as conn:
            cur = conn.execute("UPDATE visitor_events SET clip_status = ?, clip_path = ?, clip_error = ?, clip_frames = ? WHERE id = ?",
                               (status, path, error, frames, event_id))
            return cur.rowcount > 0

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


service = VisitorService()
person_detection.service.register("visitors", service.wants, service.deliver)
