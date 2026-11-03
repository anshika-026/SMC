"""
intrusion.py

Restricted-area (intrusion) detection. An admin draws a zone (a polygon on
a camera's picture, fractions of the frame) and optionally an active window
("19:00"-"08:00" = overnight only). About once a second, each camera with an
enabled zone gets tracked people from the shared per-camera detector
(person_detection.py); a person whose FEET — the
bottom-centre of their box — are inside an active zone is an intrusion. The
feet, not the box centre, so someone walking past in front of the area
doesn't count as being in it.

Each intrusion raises an "Intrusion Detected" alert (alerts.py, High) with an
annotated snapshot. While that alert is still open, further detections in
the same zone count against it (occurrences / last seen) instead of creating
new ones. Detections are also logged per zone (at most once per
EVENT_LOG_SECONDS) for the Intrusion page's charts.

Switched on and off with the "intrusion" analytics switch; cameras with
zones are kept streaming only while it's on.

Ported from MAINDV (branch intrusion-loitering, 19fd9c9: zone types, loitering
timers, active hours, evidence clips). SMC additions: a zone can belong to a
named area ("Reception") and be marked as an entry gate ("Reception Entry
Gate"); the same area + zone name drawn on several cameras is one zone watched
by those cameras (each camera keeps its own outline, since the picture differs).
In SMC this is the only restricted-area engine (it replaced restricted_zones.py).
"""

import concurrent.futures
import datetime
import json
import logging
import sqlite3
import threading
import time

import cv2
import numpy as np

from app import alerts, analytics_settings, camera_db, evidence, gate_crossing, loitering, resilience
from app.desk_tracker import in_polygon
from app import lifecycle
from app import config, db

log = logging.getLogger("intrusion")

DB_PATH = config.DB_PATH
CYCLE_SECONDS = 1.0
PERSON_CONF = 0.45
EVENT_LOG_SECONDS = 30
SYNC_INTERVAL_SECONDS = 15
ZONE_TYPES = ("restricted", "loitering")     # restricted = a person inside at all (the original behaviour)
SEVERITIES = alerts.SEVERITIES
LOITER_MIN_SECONDS = 5
LOITER_MAX_SECONDS = 24 * 3600


def _conn():
    """Shared connection policy (app/db.py): lock timeout + busy_timeout,
    commit on success, rollback on error, and always closed."""
    return db.connect(DB_PATH, row_factory=sqlite3.Row)


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intrusion_zones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                camera_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                polygon TEXT NOT NULL,
                active_from TEXT,      -- "HH:MM" or NULL = always
                active_to TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS intrusion_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone_id INTEGER NOT NULL,
                camera_id INTEGER NOT NULL,
                ts REAL NOT NULL,
                people INTEGER NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_intrusion_events_ts ON intrusion_events (ts)")
        # Zone type and per-zone alert settings. Added in place, so existing zones are untouched and
        # simply become 'restricted' zones that behave exactly as before.
        have = {r[1] for r in conn.execute("PRAGMA table_info(intrusion_zones)")}
        for col, ddl in (("zone_type", "TEXT NOT NULL DEFAULT 'restricted'"), ("dwell_seconds", "INTEGER"),
                         ("grace_seconds", "REAL"), ("severity", "TEXT"), ("alerts_enabled", "INTEGER NOT NULL DEFAULT 1"),
                         # SMC: the area a zone belongs to, and whether it is an entry gate.
                         ("area", "TEXT"), ("is_entry_gate", "INTEGER NOT NULL DEFAULT 0"),
                         # SMC: an entry gate's line and which side is inside (gate_crossing.py).
                         ("entry_line", "TEXT"), ("inside_sign", "INTEGER NOT NULL DEFAULT 1")):
            if col not in have:
                conn.execute(f"ALTER TABLE intrusion_zones ADD COLUMN {col} {ddl}")


# ---- zones ------------------------------------------------------------------

def _zone(row) -> dict:
    d = dict(row)
    d["polygon"] = json.loads(d["polygon"])
    d["enabled"] = bool(d["enabled"])
    d["alerts_enabled"] = bool(d.get("alerts_enabled", 1))
    d["zone_type"] = d.get("zone_type") or "restricted"
    d["is_entry_gate"] = bool(d.get("is_entry_gate"))
    d["area"] = d.get("area") or ""
    d["entry_line"] = json.loads(d["entry_line"]) if d.get("entry_line") else None
    d["inside_sign"] = -1 if (d.get("inside_sign") or 1) < 0 else 1
    return d


def list_zones(camera_id: int | None = None) -> list[dict]:
    q, p = "SELECT * FROM intrusion_zones", []
    if camera_id is not None:
        q, p = q + " WHERE camera_id = ?", [camera_id]
    with _conn() as conn:
        return [_zone(r) for r in conn.execute(q + " ORDER BY id", p)]


def get_zone(zone_id: int) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM intrusion_zones WHERE id = ?", (zone_id,)).fetchone()
    return _zone(row) if row else None


def create_zone(camera_id: int, name: str, polygon: list, active_from: str | None, active_to: str | None,
                zone_type: str = "restricted", dwell_seconds: int | None = None, grace_seconds: float | None = None,
                severity: str | None = None, alerts_enabled: bool = True, area: str | None = None,
                is_entry_gate: bool = False, entry_line: list | None = None, inside_sign: int = 1) -> dict:
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO intrusion_zones (camera_id, name, polygon, active_from, active_to, created_at, "
            "zone_type, dwell_seconds, grace_seconds, severity, alerts_enabled, area, is_entry_gate, entry_line, inside_sign) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (camera_id, name, json.dumps(polygon), active_from, active_to, time.time(),
             zone_type, dwell_seconds, grace_seconds, severity, int(bool(alerts_enabled)), area or None,
             int(bool(is_entry_gate)), json.dumps(entry_line) if entry_line else None, -1 if inside_sign < 0 else 1),
        )
        row = conn.execute("SELECT * FROM intrusion_zones WHERE id = ?", (cur.lastrowid,)).fetchone()
    service.refresh()
    return _zone(row)


def update_zone(zone_id: int, **fields) -> dict | None:
    allowed = {k: v for k, v in fields.items() if k in (
        "name", "active_from", "active_to", "enabled", "polygon", "zone_type", "dwell_seconds", "grace_seconds",
        "severity", "alerts_enabled", "area", "is_entry_gate", "entry_line", "inside_sign")}
    if allowed:
        if "entry_line" in allowed:
            allowed["entry_line"] = json.dumps(allowed["entry_line"]) if allowed["entry_line"] else None
        if "inside_sign" in allowed:
            allowed["inside_sign"] = -1 if (allowed["inside_sign"] or 1) < 0 else 1
        if allowed.keys() & {"polygon", "entry_line", "inside_sign", "is_entry_gate"}:
            service._gates.reset_zone(zone_id)       # a changed gate starts its crossings clean
        if "enabled" in allowed:
            allowed["enabled"] = int(bool(allowed["enabled"]))
        if "is_entry_gate" in allowed:
            allowed["is_entry_gate"] = int(bool(allowed["is_entry_gate"]))
        if "area" in allowed:
            allowed["area"] = (allowed["area"] or "").strip() or None
        if "alerts_enabled" in allowed:
            allowed["alerts_enabled"] = int(bool(allowed["alerts_enabled"]))
        if "polygon" in allowed:
            allowed["polygon"] = json.dumps(allowed["polygon"])
        if allowed.keys() & {"polygon", "zone_type", "dwell_seconds", "grace_seconds"}:
            service._loiter.reset_zone(zone_id)      # a changed outline or duration starts the timers clean
        with _conn() as conn:
            conn.execute(f"UPDATE intrusion_zones SET {', '.join(f'{k} = ?' for k in allowed)} WHERE id = ?", (*allowed.values(), zone_id))
    service.refresh()
    with _conn() as conn:
        row = conn.execute("SELECT * FROM intrusion_zones WHERE id = ?", (zone_id,)).fetchone()
    return _zone(row) if row else None


def delete_zone(zone_id: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM intrusion_zones WHERE id = ?", (zone_id,))
    service.refresh()


def is_active(zone: dict, now: float | None = None) -> bool:
    """Inside the zone's active window (which may wrap past midnight)."""
    if not zone["enabled"]:
        return False
    start, end = zone.get("active_from"), zone.get("active_to")
    if not start or not end:
        return True
    t = datetime.datetime.fromtimestamp(now if now is not None else time.time()).strftime("%H:%M")
    return start <= t < end if start <= end else (t >= start or t < end)


def events_list(start_ts: float, end_ts: float, camera_ids: set[int] | None = None, limit: int = 5000) -> list[dict]:
    """Itemized intrusion events in [start_ts, end_ts], newest first, scoped to a
    tenant's cameras (None = all). This is the per-detection truth (the Alerts
    view dedupes open alerts per zone and under-reports)."""
    cam_sql, cam_params = ("", [])
    if camera_ids is not None:
        if not camera_ids:
            return []
        cam_sql = f" AND e.camera_id IN ({','.join('?' * len(camera_ids))})"
        cam_params = sorted(camera_ids)
    with _conn() as conn:
        rows = conn.execute(
            "SELECT e.id, e.camera_id, e.zone_id, e.ts, e.people, z.name AS zone_name "
            "FROM intrusion_events e LEFT JOIN intrusion_zones z ON z.id = e.zone_id "
            "WHERE e.ts >= ? AND e.ts <= ?" + cam_sql + " ORDER BY e.ts DESC LIMIT ?",
            (start_ts, end_ts, *cam_params, limit),
        ).fetchall()
    cameras = {c["id"]: c["name"] for c in camera_db.list_cameras()}
    return [{"id": r["id"], "ts": r["ts"], "camera_id": r["camera_id"],
             "camera": cameras.get(r["camera_id"], f"Camera {r['camera_id']}"),
             "zone": r["zone_name"] or f"Zone {r['zone_id']}", "people": r["people"]} for r in rows]


def stats(days: int = 7, now: float | None = None, camera_ids: set[int] | None = None) -> dict:
    """camera_ids scopes the stats to a tenant's cameras (T10). None = all
    (admin); an empty set = a client with no cameras, so no events."""
    now = now if now is not None else time.time()
    today = datetime.date.fromtimestamp(now)
    start = datetime.datetime.combine(today - datetime.timedelta(days=days - 1), datetime.time()).timestamp()
    day_start = datetime.datetime.combine(today, datetime.time()).timestamp()
    cam_sql, cam_params = ("", [])
    if camera_ids is not None:
        cam_sql = f" AND camera_id IN ({','.join('?' * len(camera_ids))})" if camera_ids else " AND 0"
        cam_params = sorted(camera_ids)
    with _conn() as conn:
        rows = conn.execute("SELECT zone_id, ts FROM intrusion_events WHERE ts >= ?" + cam_sql,
                            (start, *cam_params)).fetchall()
    per_day = {(today - datetime.timedelta(days=i)).isoformat(): 0 for i in range(days)}
    per_zone_today: dict[int, int] = {}
    last_seen: dict[int, float] = {}
    for zone_id, ts in rows:
        d = datetime.date.fromtimestamp(ts).isoformat()
        if d in per_day:
            per_day[d] += 1
        if ts >= day_start:
            per_zone_today[zone_id] = per_zone_today.get(zone_id, 0) + 1
        last_seen[zone_id] = max(last_seen.get(zone_id, 0), ts)
    return {
        "per_day": [{"date": d, "day": datetime.date.fromisoformat(d).strftime("%a"), "count": n} for d, n in sorted(per_day.items())],
        "per_zone_today": per_zone_today,
        "last_seen": last_seen,
        "today": sum(per_zone_today.values()),
    }


# ---- alert history and analytics (SMC Intrusion page) -------------------------

ALERT_EVENTS = ("Intrusion Detected", "Loitering Detected")
# The Intrusion page's alert-type filter: intrusion (restricted areas / entry gates) or loitering.
ALERT_TYPES = {"intrusion": "Intrusion Detected", "loitering": "Loitering Detected"}


def list_alerts(start_ts: float, end_ts: float, camera_id: int | None = None, zone_id: int | None = None,
                status: str | None = None, q: str = "", limit: int = 2000, alert_type: str | None = None) -> list[dict]:
    """Intrusion and loitering alerts (alerts.py rows) in [start_ts, end_ts], newest first.
    duration_seconds: how long the intrusion lasted as far as the alert knows (first detection to
    the last one counted against it); for a loitering alert, at least the zone's duration."""
    events = (ALERT_TYPES[alert_type],) if alert_type else ALERT_EVENTS
    sql = (f"SELECT * FROM alerts WHERE event IN ({','.join('?' * len(events))}) AND ts >= ? AND ts <= ?")
    params: list = [*events, start_ts, end_ts]
    if camera_id is not None:
        sql, params = sql + " AND camera_id = ?", params + [camera_id]
    if zone_id is not None:
        sql, params = sql + " AND zone_id = ?", params + [zone_id]
    if status:
        sql, params = sql + " AND status = ?", params + [status]
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(sql + " ORDER BY ts DESC LIMIT ?", (*params, limit))]
    cams = {c["id"]: c for c in camera_db.list_cameras()}
    zones = {z["id"]: z for z in list_zones()}
    out = []
    needle = (q or "").strip().lower()
    for r in rows:
        cam = cams.get(r["camera_id"]) or {}
        zone = zones.get(r["zone_id"]) or {}
        duration = max(0.0, (r["last_seen"] or r["ts"]) - r["ts"])
        if r["event"] == "Loitering Detected" and zone.get("dwell_seconds"):
            duration = max(duration, float(zone["dwell_seconds"]))
        item = {
            "id": r["id"], "event": r["event"], "severity": r["severity"], "status": r["status"],
            "ts": r["ts"], "last_seen": r["last_seen"], "occurrences": r["occurrences"],
            "duration_seconds": duration, "message": r["message"] or "",
            "camera_id": r["camera_id"], "camera_name": cam.get("name") or f"Camera {r['camera_id']}",
            "site": cam.get("site") or "", "zone_id": r["zone_id"], "zone_name": r["zone_name"] or zone.get("name") or "",
            "area": zone.get("area") or "", "is_entry_gate": bool(zone.get("is_entry_gate")),
            "zone_type": zone.get("zone_type") or ("loitering" if r["event"] == "Loitering Detected" else "restricted"),
            "clip_status": r.get("clip_status"), "has_clip": r.get("clip_status") == "ready",
            "has_snapshot": bool(r.get("snapshot_path")),
        }
        if needle and needle not in " ".join(str(item[k]) for k in ("camera_name", "zone_name", "area", "message", "event", "site")).lower():
            continue
        out.append(item)
    return out


def analytics(start_ts: float, end_ts: float, camera_id: int | None = None, zone_id: int | None = None,
              alert_type: str | None = None) -> dict:
    """Business view of real intrusion records over a date range: totals, by camera, by zone /
    area, by hour of day (peak times) and by day, plus the same totals for the previous period of
    equal length (to say whether incidents are rising or falling). Counts are alerts (one per
    intrusion; repeat detections while it is open count against the same alert), with the
    itemized detections (intrusion_events) alongside."""
    items = list_alerts(start_ts, end_ts, camera_id=camera_id, zone_id=zone_id, limit=100_000, alert_type=alert_type)
    length = end_ts - start_ts
    prev = list_alerts(start_ts - length - 1, start_ts - 1, camera_id=camera_id, zone_id=zone_id, limit=100_000,
                       alert_type=alert_type)
    ev_sql, ev_params = "SELECT zone_id, camera_id, ts FROM intrusion_events WHERE ts >= ? AND ts <= ?", [start_ts, end_ts]
    if camera_id is not None:
        ev_sql, ev_params = ev_sql + " AND camera_id = ?", ev_params + [camera_id]
    if zone_id is not None:
        ev_sql, ev_params = ev_sql + " AND zone_id = ?", ev_params + [zone_id]
    with _conn() as conn:
        detections = conn.execute(ev_sql, ev_params).fetchall()

    def count_by(key):
        c: dict = {}
        for it in items:
            k = key(it)
            c[k] = c.get(k, 0) + 1
        return sorted(({"name": k, "count": n} for k, n in c.items()), key=lambda x: -x["count"])

    first = datetime.date.fromtimestamp(start_ts)
    last = datetime.date.fromtimestamp(min(end_ts, time.time()))
    per_day = {(first + datetime.timedelta(days=i)).isoformat(): 0 for i in range((last - first).days + 1)}
    per_hour = [0] * 24
    for it in items:
        d = datetime.datetime.fromtimestamp(it["ts"])
        per_day[d.date().isoformat()] = per_day.get(d.date().isoformat(), 0) + 1
        per_hour[d.hour] += 1
    peak = max(range(24), key=lambda h: per_hour[h]) if items else None
    durations = [it["duration_seconds"] for it in items]
    return {
        "total": len(items),
        "intrusions": sum(1 for it in items if it["event"] == "Intrusion Detected"),
        "loitering": sum(1 for it in items if it["event"] == "Loitering Detected"),
        "open": sum(1 for it in items if it["status"] != "Resolved"),
        "detections": len(detections),
        "average_duration_seconds": (sum(durations) / len(durations)) if durations else None,
        "with_clip": sum(1 for it in items if it["has_clip"]),
        "by_camera": count_by(lambda it: it["camera_name"]),
        "by_zone": count_by(lambda it: it["zone_name"] or "Zone"),
        "by_area": count_by(lambda it: it["area"] or "No area"),
        "by_hour": [{"hour": h, "label": f"{h:02d}:00", "count": per_hour[h]} for h in range(24)],
        "peak_hour": peak,
        "per_day": [{"date": d, "count": n} for d, n in sorted(per_day.items())],
        "previous": {
            "start": datetime.date.fromtimestamp(start_ts - length - 1).isoformat(),
            "end": datetime.date.fromtimestamp(start_ts - 1).isoformat(),
            "total": len(prev),
            "intrusions": sum(1 for it in prev if it["event"] == "Intrusion Detected"),
            "loitering": sum(1 for it in prev if it["event"] == "Loitering Detected"),
        },
    }


# ---- live detection ---------------------------------------------------------

class _KeepAliveSink:
    def put_nowait(self, item):
        pass


class IntrusionService:
    def __init__(self):
        self._zones: dict[int, list[dict]] = {}
        self._executors: dict[int, concurrent.futures.ThreadPoolExecutor] = {}
        self._futures: dict[int, concurrent.futures.Future] = {}
        self._last_logged: dict[int, float] = {}
        self._sinks: dict[int, _KeepAliveSink] = {}
        self._health: dict[int, resilience.FeatureHealth] = {}
        self._loiter = loitering.LoiterTracker()
        self._gates = gate_crossing.GateCounter()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.refresh()
        threading.Thread(target=self._sync_loop, daemon=True, name="intrusion-keepalive").start()

    def refresh(self) -> None:
        by_cam: dict[int, list[dict]] = {}
        for z in list_zones():
            if z["enabled"]:
                by_cam.setdefault(z["camera_id"], []).append(z)
        self._zones = by_cam
        self._loiter.keep_only({z["id"] for zs in by_cam.values() for z in zs if z["zone_type"] == "loitering"})
        self._gates.keep_only({z["id"] for zs in by_cam.values() for z in zs if is_counting_gate(z)})
        if self._started:
            # SMC: start streaming / buffering a newly guarded camera now, not at the next
            # keep-alive pass (up to SYNC_INTERVAL_SECONDS later), so the first alert of a new
            # zone already has footage for its evidence clip.
            try:
                self.sync()
                evidence.service.sync(set(self._sinks))
            except Exception:
                log.exception("intrusion: immediate sync after a zone change failed")

    def _sync_loop(self) -> None:
        while True:
            try:
                self.sync()
                evidence.service.sync(set(self._sinks))   # one rolling frame buffer per guarded camera
            except Exception:
                log.exception("intrusion: keep-alive sync failed")
            if lifecycle.wait(SYNC_INTERVAL_SECONDS):
                return
    def sync(self) -> None:
        from app import camera_stream

        wanted = set(self._zones) if analytics_settings.enabled("intrusion") else set()
        wanted &= {c["id"] for c in camera_db.list_cameras() if camera_db.is_streamable(c)}
        for cid in wanted - self._sinks.keys():
            sink = _KeepAliveSink()
            camera_stream.get_stream(cid).subscribe(sink, is_collector=True, wants_frames=False)
            self._sinks[cid] = sink
        for cid in self._sinks.keys() - wanted:
            camera_stream.get_stream(cid).unsubscribe(self._sinks.pop(cid))

    # People come from the shared per-camera detector (person_detection.py),
    # about once a second on cameras with a zone inside its active hours.
    def wants(self, camera_id: int) -> float:
        zones = self._zones.get(camera_id)
        if not zones or not analytics_settings.enabled("intrusion") or not self.health(camera_id).allow():
            return 0.0
        return 1.0 / CYCLE_SECONDS if any(is_active(z) for z in zones) else 0.0

    def deliver(self, camera_id: int, frame: np.ndarray, people: list[dict], ts: float) -> None:
        fut = self._futures.get(camera_id)
        if fut is not None and not fut.done():
            return
        active = [z for z in self._zones.get(camera_id, []) if is_active(z, ts)]
        if not active:
            return
        boxes = [p["bbox"] for p in people if p["confidence"] >= PERSON_CONF]
        ex = self._executors.setdefault(
            camera_id, concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"intrusion-{camera_id}"))
        self._futures[camera_id] = ex.submit(self._detect, camera_id, frame, active, ts, boxes, people)

    def health(self, camera_id: int) -> resilience.FeatureHealth:
        h = self._health.get(camera_id)
        if h is None:
            h = self._health.setdefault(camera_id, resilience.health_for("intrusion", camera_id))
        return h

    def _detect(self, camera_id: int, frame: np.ndarray, zones: list[dict], now: float, boxes: list,
                people: list | None = None) -> None:
        # Runs on an executor: an exception here would otherwise vanish into
        # the Future unlogged. Logged and retried on a backoff instead.
        try:
            self._detect_inner(camera_id, frame, zones, now, boxes, people or [])
            self._loiter_inner(camera_id, frame, zones, now, people or [])
        except Exception as e:
            self.health(camera_id).failure(e)
        else:
            self.health(camera_id).success()

    def _detect_inner(self, camera_id: int, frame: np.ndarray, zones: list[dict], now: float, boxes: list,
                      people: list | None = None) -> None:
        zones = [z for z in zones if z.get("zone_type", "restricted") != "loitering"]   # handled by _loiter_inner
        h, w = frame.shape[:2]
        # SMC: an entry gate with a line marks each person who comes IN (gate_crossing.py), never
        # people going out; it runs even on an empty frame so someone who walked in out of view counts.
        for zone in [z for z in zones if is_counting_gate(z)]:
            tracked = [p for p in (people or []) if p.get("track_id") is not None and p["confidence"] >= PERSON_CONF]
            for entry in self._gates.observe(zone["id"], zone["entry_line"], zone["inside_sign"], tracked, w, h, now,
                                             area=zone["polygon"]):
                self._mark_entry(camera_id, frame, zone, entry, now)
        zones = [z for z in zones if not is_counting_gate(z)]
        if not boxes:
            return
        for zone in zones:
            inside = [b for b in boxes if in_polygon((b[0] + b[2]) / 2 / w, b[3] / h, zone["polygon"])]
            if not inside:
                continue
            if now - self._last_logged.get(zone["id"], 0) >= EVENT_LOG_SECONDS:
                self._last_logged[zone["id"]] = now
                with _conn() as conn:
                    conn.execute("INSERT INTO intrusion_events (zone_id, camera_id, ts, people) VALUES (?, ?, ?, ?)",
                                 (zone["id"], camera_id, now, len(inside)))
            if not zone.get("alerts_enabled", True):
                continue                                  # this zone's alerts are switched off; events are still logged
            alerts.raise_alert(
                "Intrusion Detected", zone.get("severity") or "High", camera_id,
                message=f"{len(inside)} {'person' if len(inside) == 1 else 'people'} inside {zone['name']}",
                dedupe_key=f"intrusion:{zone['id']}", snapshot_jpeg=_annotate(frame, zone, inside), now=now,
                zone_id=zone["id"], zone_name=zone["name"],
                on_created=lambda aid: evidence.service.schedule(aid, camera_id, now),
            )

    def _mark_entry(self, camera_id: int, frame: np.ndarray, zone: dict, entry: dict, now: float) -> None:
        """One person came in through an entry gate: logged, and (alerts on) their own alert + clip."""
        with _conn() as conn:
            conn.execute("INSERT INTO intrusion_events (zone_id, camera_id, ts, people) VALUES (?, ?, ?, 1)",
                         (zone["id"], camera_id, now))
        if not zone.get("alerts_enabled", True):
            return
        alerts.raise_alert(
            "Intrusion Detected", zone.get("severity") or "High", camera_id,
            message=f"1 person came in through {zone['name']}",
            snapshot_jpeg=_annotate(frame, zone, [entry["bbox"]]), now=now,
            zone_id=zone["id"], zone_name=zone["name"],
            on_created=lambda aid: evidence.service.schedule(aid, camera_id, now),
        )

    def _loiter_inner(self, camera_id: int, frame: np.ndarray, zones: list[dict], now: float, people: list) -> None:
        """Loitering zones: a tracked person who stays inside one for its duration raises ONE alert
        per continuous stay (see loitering.py for the lifecycle). Uses the same people the shared
        detector already delivered, so there is no extra inference."""
        active = {z["id"]: z for z in zones if z.get("zone_type") == "loitering"}
        known = {z["id"]: z for z in self._zones.get(camera_id, []) if z["zone_type"] == "loitering"}
        known.update(active)
        if not known:
            return
        h, w = frame.shape[:2]
        tracked = [p for p in people if p.get("track_id") is not None and p["confidence"] >= PERSON_CONF]
        inputs, where = [], {}
        for zid, zone in known.items():
            if not zone.get("dwell_seconds"):
                continue                                  # misconfigured: no duration, nothing to wait for
            inside = set()
            if zid in active:                             # outside its hours nobody is "inside", so timers lapse
                for p in tracked:
                    b = p["bbox"]
                    if in_polygon((b[0] + b[2]) / 2 / w, b[3] / h, zone["polygon"]):
                        inside.add(p["track_id"])
                        where[(zid, p["track_id"])] = b
            grace = zone.get("grace_seconds")
            inputs.append((zid, float(zone["dwell_seconds"]),
                           loitering.DEFAULT_GRACE_SECONDS if grace is None else float(grace), inside))
        for zid, tid, seconds in self._loiter.observe(camera_id, inputs):
            zone = known[zid]
            if not zone.get("alerts_enabled", True):
                continue
            alerts.raise_alert(
                "Loitering Detected", zone.get("severity") or "Medium", camera_id,
                message=f"Someone stayed in {zone['name']} for {format_duration(seconds)} (limit {format_duration(zone['dwell_seconds'])})",
                snapshot_jpeg=_annotate(frame, zone, [where[(zid, tid)]]), now=now,
                zone_id=zone["id"], zone_name=zone["name"],
                on_created=lambda aid: evidence.service.schedule(aid, camera_id, now),
            )


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds >= 3600 and seconds % 3600 == 0:
        n = seconds // 3600
        return f"{n} hour{'s' if n != 1 else ''}"
    if seconds >= 60 and seconds % 60 == 0:
        n = seconds // 60
        return f"{n} minute{'s' if n != 1 else ''}"
    if seconds >= 60:
        return f"{seconds // 60} min {seconds % 60} s"
    return f"{seconds} second{'s' if seconds != 1 else ''}"


def is_counting_gate(zone: dict) -> bool:
    """An entry gate with a gate line: it marks people coming in, not presence."""
    return bool(zone.get("is_entry_gate") and zone.get("entry_line"))


def _annotate(frame: np.ndarray, zone: dict, boxes) -> bytes | None:
    img = frame.copy()
    h, w = img.shape[:2]
    pts = np.array([[int(x * w), int(y * h)] for x, y in zone["polygon"]], dtype=np.int32)
    overlay = img.copy()
    cv2.fillPoly(overlay, [pts], (60, 60, 230))
    img = cv2.addWeighted(overlay, 0.25, img, 0.75, 0)
    cv2.polylines(img, [pts], True, (60, 60, 230), 3)
    if zone.get("entry_line"):
        (lx1, ly1), (lx2, ly2) = zone["entry_line"]
        cv2.line(img, (int(lx1 * w), int(ly1 * h)), (int(lx2 * w), int(ly2 * h)), (21, 204, 250), 4)
    for b in boxes:
        x1, y1, x2, y2 = (int(v) for v in b)
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 0, 255), 3)
    if w > 1280:
        img = cv2.resize(img, (1280, round(h * 1280 / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return buf.tobytes() if ok else None


service = IntrusionService()

from app import person_detection  # noqa: E402

person_detection.service.register("intrusion", service.wants, service.deliver)
