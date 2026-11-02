"""
restricted_zones.py

Restricted areas: a polygon marked on a camera where people should not be.
When someone's feet (bottom-centre of their tracked box) are inside it for
ENTRY_CONFIRM_LOOKS looks in a row, an alert is raised in the existing Alerts
system (alerts.raise_alert), according to the zone's rule:

  everyone        anyone entering raises an alert
  non_employees   only people not recognised as an enrolled employee; the face
                  pipeline gets IDENTIFY_GRACE_SECONDS to name them first (its
                  live identities only: no extra face inference here)

At most one alert per zone per cooldown_minutes; while an alert is still open,
repeats are counted on it (alerts' dedupe) instead of raising new ones. Every
entry - alerted or not - is kept as zone activity (restricted_events), with
who it was if they were recognised, so the zone's history can be reviewed.

Works on the shared person detector (person_detection.py), like Visitor
Management and footfall; a camera with no enabled restricted zone costs
nothing. Separate from visitor rules: an employee in a visitor seating area is
never a visitor, and restricted areas never change visitor or attendance data.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import sqlite3
import threading
import time

import cv2
import numpy as np

from app import alerts, audit, camera_db, config, db, lifecycle, master_link, person_detection, resilience
from app.desk_tracker import in_polygon

log = logging.getLogger("restricted_zones")

DB_PATH = config.DB_PATH
DETECT_FPS = float(os.environ.get("RESTRICTED_DETECT_FPS", "2"))
PERSON_CONF = float(os.environ.get("RESTRICTED_PERSON_CONF", "0.45"))
ENTRY_CONFIRM_LOOKS = 2
IDENTIFY_GRACE_SECONDS = float(os.environ.get("RESTRICTED_IDENTIFY_GRACE_SECONDS", "5"))
EXIT_GRACE_SECONDS = 10.0
HOUSEKEEPING_SECONDS = 5
RULES = {"everyone": "Alert on anyone", "non_employees": "Alert on people who are not recognised employees"}
SEVERITIES = ("Critical", "High", "Medium", "Low")


def _conn():
    return db.connect(DB_PATH, row_factory=sqlite3.Row)


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS restricted_zones (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   camera_id INTEGER NOT NULL,
                   name TEXT NOT NULL,
                   polygon TEXT NOT NULL,              -- JSON [[x, y], ...] fractions of the frame
                   rule TEXT NOT NULL DEFAULT 'non_employees',
                   severity TEXT NOT NULL DEFAULT 'High',
                   cooldown_minutes REAL NOT NULL DEFAULT 10,
                   enabled INTEGER NOT NULL DEFAULT 1,
                   created_at REAL NOT NULL,
                   updated_at REAL NOT NULL)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS restricted_events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   zone_id INTEGER NOT NULL,
                   camera_id INTEGER NOT NULL,
                   track_id INTEGER,
                   entered_at REAL NOT NULL,
                   last_seen REAL NOT NULL,
                   left_at REAL,
                   employee_id TEXT,                   -- recognised as this employee (NULL = not recognised)
                   alert_id INTEGER)                   -- the alert it raised or added to (NULL = none)"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_restricted_events_zone ON restricted_events (zone_id, entered_at)")


# --------------------------------------------------------------------------- zones

def _zone(r) -> dict:
    d = dict(r)
    d["polygon"] = json.loads(d["polygon"])
    d["enabled"] = bool(d["enabled"])
    d["rule_label"] = RULES.get(d["rule"], d["rule"])
    return d


def _check(name, polygon, rule, severity, cooldown) -> tuple:
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Give the zone a name.")
    if not isinstance(polygon, list) or len(polygon) < 3:
        raise ValueError("Mark at least 3 points for the zone.")
    pts = []
    for p in polygon:
        x, y = float(p[0]), float(p[1])
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ValueError("Zone points must be inside the picture.")
        pts.append([round(x, 4), round(y, 4)])
    if rule not in RULES:
        raise ValueError("Unknown rule.")
    if severity not in SEVERITIES:
        raise ValueError("Unknown severity.")
    cooldown = float(cooldown)
    if not 1 <= cooldown <= 24 * 60:
        raise ValueError("Cooldown must be between 1 minute and 24 hours.")
    return name, pts, rule, severity, cooldown


def list_zones(camera_id: int | None = None) -> list[dict]:
    q, params = "SELECT * FROM restricted_zones", []
    if camera_id is not None:
        q += " WHERE camera_id = ?"
        params.append(camera_id)
    with _conn() as conn:
        return [_zone(r) for r in conn.execute(q + " ORDER BY id", params)]


def get_zone(zone_id: int) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM restricted_zones WHERE id = ?", (zone_id,)).fetchone()
    return None if r is None else _zone(r)


def create_zone(camera_id: int, name: str, polygon: list, rule: str = "non_employees", severity: str = "High",
                cooldown_minutes: float = 10, enabled: bool = True, actor: dict | None = None) -> dict:
    if camera_db.get_camera(camera_id) is None:
        raise ValueError("Camera not found.")
    name, pts, rule, severity, cooldown = _check(name, polygon, rule, severity, cooldown_minutes)
    now = time.time()
    with _conn() as conn:
        cur = conn.execute("INSERT INTO restricted_zones (camera_id, name, polygon, rule, severity, cooldown_minutes, enabled, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (camera_id, name, json.dumps(pts), rule, severity, cooldown, int(enabled), now, now))
        zid = cur.lastrowid
    audit.record("restricted_zone.create", actor, f"zone:{zid}", camera_id=camera_id, name=name, rule=rule)
    service.refresh()
    return get_zone(zid)


def update_zone(zone_id: int, fields: dict, actor: dict | None = None) -> dict:
    z = get_zone(zone_id)
    if z is None:
        raise KeyError(zone_id)
    merged = {**z, **{k: v for k, v in fields.items() if v is not None}}
    name, pts, rule, severity, cooldown = _check(merged["name"], merged["polygon"], merged["rule"], merged["severity"], merged["cooldown_minutes"])
    with _conn() as conn:
        conn.execute("UPDATE restricted_zones SET name = ?, polygon = ?, rule = ?, severity = ?, cooldown_minutes = ?, enabled = ?, updated_at = ? WHERE id = ?",
                     (name, json.dumps(pts), rule, severity, cooldown, int(bool(merged["enabled"])), time.time(), zone_id))
    audit.record("restricted_zone.update", actor, f"zone:{zone_id}", fields=sorted(k for k, v in fields.items() if v is not None))
    service.refresh()
    return get_zone(zone_id)


def delete_zone(zone_id: int, actor: dict | None = None) -> bool:
    """Removes the zone; its activity history is kept."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM restricted_zones WHERE id = ?", (zone_id,))
    if cur.rowcount:
        audit.record("restricted_zone.delete", actor, f"zone:{zone_id}")
        service.refresh()
    return cur.rowcount > 0


def activity(zone_id: int | None = None, since: float | None = None, limit: int = 100) -> list[dict]:
    q, params = "SELECT * FROM restricted_events WHERE 1 = 1", []
    if zone_id is not None:
        q += " AND zone_id = ?"
        params.append(zone_id)
    if since is not None:
        q += " AND entered_at >= ?"
        params.append(since)
    with _conn() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY entered_at DESC LIMIT ?", (*params, limit))]


def activity_counts(since: float) -> dict[int, dict]:
    """zone id -> {entries, alerts, last_at} since `since`."""
    with _conn() as conn:
        rows = conn.execute("SELECT zone_id, COUNT(*) AS n, SUM(alert_id IS NOT NULL) AS alerted, MAX(entered_at) AS last_at "
                            "FROM restricted_events WHERE entered_at >= ? GROUP BY zone_id", (since,)).fetchall()
    return {r["zone_id"]: {"entries": r["n"], "alerts": r["alerted"] or 0, "last_at": r["last_at"]} for r in rows}


# --------------------------------------------------------------------------- monitoring

class _KeepAliveSink:
    def put_nowait(self, item):
        pass


class _Presence:
    __slots__ = ("looks", "event_id", "first", "last", "employee_id", "alerted")

    def __init__(self, ts):
        self.looks, self.event_id, self.first, self.last = 0, None, ts, ts
        self.employee_id, self.alerted = None, False


class RestrictedZoneService:
    def __init__(self):
        self._zones: dict[int, list[dict]] = {}                 # camera -> enabled zones
        self._present: dict[tuple[int, int], _Presence] = {}    # (zone, track) -> presence
        self._last_alert: dict[int, float] = {}                 # zone -> when it last raised an alert
        self._lock = threading.RLock()
        self._executors: dict[int, concurrent.futures.ThreadPoolExecutor] = {}
        self._futures: dict[int, concurrent.futures.Future] = {}
        self._health: dict[int, resilience.FeatureHealth] = {}
        self._sinks: dict[int, _KeepAliveSink] = {}
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.refresh()
        threading.Thread(target=self._housekeeping, daemon=True, name="restricted-housekeeping").start()

    def refresh(self) -> None:
        zones: dict[int, list[dict]] = {}
        try:
            for z in list_zones():
                if z["enabled"]:
                    zones.setdefault(z["camera_id"], []).append(z)
        except sqlite3.OperationalError:
            return
        with self._lock:
            self._zones = zones
            live = {z["id"] for zs in zones.values() for z in zs}
            for key in [k for k in self._present if k[0] not in live]:
                self._present.pop(key, None)
        if self._started:
            self._sync()

    def _sync(self) -> None:
        """Keep cameras with a restricted zone streaming (no viewer needed)."""
        from app import camera_stream

        wanted = {c for c in self._zones} & {c["id"] for c in camera_db.list_cameras() if camera_db.is_streamable(c)}
        for cid in wanted - self._sinks.keys():
            sink = _KeepAliveSink()
            camera_stream.get_stream(cid).subscribe(sink, is_collector=True, wants_frames=False)
            self._sinks[cid] = sink
        for cid in self._sinks.keys() - wanted:
            camera_stream.get_stream(cid).unsubscribe(self._sinks.pop(cid))

    def _housekeeping(self) -> None:
        while True:
            try:
                self._sync()
                self.close_departed(time.time())
            except Exception:
                log.exception("restricted zones: housekeeping failed")
            if lifecycle.wait(HOUSEKEEPING_SECONDS):
                return

    def health(self, camera_id: int) -> resilience.FeatureHealth:
        h = self._health.get(camera_id)
        if h is None:
            h = self._health.setdefault(camera_id, resilience.health_for("restricted_zones", camera_id))
        return h

    def wants(self, camera_id: int) -> float:
        if not master_link.feature_allowed("intrusion_detection"):
            return 0.0   # not in the SMC license: zones are kept, not watched
        return DETECT_FPS if self._zones.get(camera_id) and self.health(camera_id).allow() else 0.0

    def deliver(self, camera_id: int, frame: np.ndarray, people: list[dict], ts: float) -> None:
        fut = self._futures.get(camera_id)
        if fut is not None and not fut.done():
            return
        ex = self._executors.setdefault(camera_id, concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"restricted-{camera_id}"))
        self._futures[camera_id] = ex.submit(self._process, camera_id, frame, people, ts)

    def _process(self, camera_id, frame, people, ts) -> None:
        try:
            h, w = frame.shape[:2]
            self.process(camera_id, w, h, people, ts, frame=frame)
        except Exception as e:
            self.health(camera_id).failure(e)
        else:
            self.health(camera_id).success()

    @staticmethod
    def identify(camera_id: int, bbox, w: int, h: int) -> str | None:
        """The enrolled employee the face pipeline has identified at this box
        right now (its live tracks; no inference), or None."""
        from app import visitors

        return visitors.service.identify(camera_id, bbox, w, h)

    def process(self, camera_id: int, w: int, h: int, people: list[dict], ts: float, frame=None) -> None:
        zones = self._zones.get(camera_id) or []
        if not zones:
            return
        with self._lock:
            for z in zones:
                for p in people:
                    tid = p.get("track_id")
                    if tid is None or p.get("confidence", 0) < PERSON_CONF:
                        continue
                    b = p["bbox"]
                    if not in_polygon((b[0] + b[2]) / 2 / w, b[3] / h, z["polygon"]):
                        continue
                    pr = self._present.setdefault((z["id"], tid), _Presence(ts))
                    pr.looks += 1
                    pr.last = ts
                    if pr.looks < ENTRY_CONFIRM_LOOKS:
                        continue
                    if pr.employee_id is None:
                        pr.employee_id = self.identify(camera_id, b, w, h)
                    if pr.event_id is None:
                        pr.event_id = self._open_event(z, camera_id, tid, pr.first, ts)
                    if pr.employee_id:
                        self._set_employee(pr.event_id, pr.employee_id)
                    if not pr.alerted and self._should_alert(z, pr, ts):
                        pr.alerted = True
                        self._alert(z, camera_id, pr, b, ts, frame)
            self.close_departed(ts, camera_id)

    def _should_alert(self, z: dict, pr: _Presence, ts: float) -> bool:
        if z["rule"] == "non_employees":
            if pr.employee_id:
                return False   # a recognised employee: logged, never alerted
            if ts - pr.first < IDENTIFY_GRACE_SECONDS:
                return False   # give face recognition its moment to name them
        return True

    def _alert(self, z, camera_id, pr, bbox, ts, frame) -> None:
        cooldown = z["cooldown_minutes"] * 60
        alert_id = None
        if ts - self._last_alert.get(z["id"], 0) >= cooldown:
            from app import employee_directory

            cam = camera_db.get_camera(camera_id) or {}
            who = (employee_directory.get_display(pr.employee_id)[0] or pr.employee_id) if pr.employee_id else "A person who is not a recognised employee"
            snap = None
            if frame is not None:
                try:
                    img = frame.copy()
                    x1, y1, x2, y2 = (int(v) for v in bbox)
                    cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 3)
                    hh, ww = img.shape[:2]
                    if ww > 960:
                        img = cv2.resize(img, (960, round(hh * 960 / ww)))
                    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    snap = buf.tobytes() if ok else None
                except Exception:
                    snap = None
            alert_id = alerts.raise_alert(
                "Restricted area entered", z["severity"], camera_id=camera_id,
                message=f"{who} entered “{z['name']}” on {cam.get('name') or f'Camera {camera_id}'}.",
                dedupe_key=f"restricted-zone-{z['id']}", employee_id=pr.employee_id, snapshot_jpeg=snap, now=ts) or None
            self._last_alert[z["id"]] = ts
        if alert_id:
            with _conn() as conn:
                conn.execute("UPDATE restricted_events SET alert_id = ? WHERE id = ?", (alert_id, pr.event_id))

    def _open_event(self, z, camera_id, tid, first, ts) -> int:
        with _conn() as conn:
            return conn.execute("INSERT INTO restricted_events (zone_id, camera_id, track_id, entered_at, last_seen) VALUES (?, ?, ?, ?, ?)",
                                (z["id"], camera_id, tid, first, ts)).lastrowid

    def _set_employee(self, event_id, employee_id) -> None:
        with _conn() as conn:
            conn.execute("UPDATE restricted_events SET employee_id = ? WHERE id = ? AND employee_id IS NULL", (employee_id, event_id))

    def close_departed(self, now: float, camera_id: int | None = None) -> None:
        with self._lock:
            for key, pr in list(self._present.items()):
                if camera_id is not None and not any(z["id"] == key[0] for z in self._zones.get(camera_id, [])):
                    continue
                if now - pr.last > EXIT_GRACE_SECONDS:
                    self._present.pop(key, None)
                    if pr.event_id:
                        with _conn() as conn:
                            conn.execute("UPDATE restricted_events SET left_at = ?, last_seen = ? WHERE id = ?", (pr.last, pr.last, pr.event_id))


service = RestrictedZoneService()
person_detection.service.register("restricted_zones", service.wants, service.deliver)
