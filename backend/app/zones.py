"""
zones.py

One list of every area marked on a camera, whatever feature owns it, for the
Zones & Areas screen:

  employee      desk zones (Workforce Insights): who sits at which desk
  visitor       visitor seating areas (Visitor Management): unknown people
                waiting raise visitor alerts; recognised employees never do
  restricted    intrusion zones (intrusion.py, ported from MAINDV): restricted
                areas and entry gates alert on entry inside their active
                hours; loitering zones alert after a set time inside
  entry_exit    the main gate line (Staff Count) and footfall gates (entry
                line and / or counting area)

Each feature keeps its own table, rules and detection; this module only reads
them and reports today's activity per zone. Nothing here changes how a zone
behaves, so employee, visitor, restricted and gate logic stay separate.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import time

from app import camera_db, config, db

DB_PATH = config.DB_PATH

TYPES = {
    "employee": "Employee zone",
    "visitor": "Visitor zone",
    "restricted": "Restricted zone",
    "entry_exit": "Entry / exit zone",
}


def _conn():
    return db.connect(DB_PATH, row_factory=sqlite3.Row)


def _day_start(now: float) -> float:
    return datetime.datetime.combine(datetime.date.fromtimestamp(now), datetime.time()).timestamp()


def _safe(fn, default):
    try:
        return fn()
    except sqlite3.OperationalError:
        return default   # that feature was never set up on this database


def list_all(now: float | None = None) -> dict:
    from app import desk_db, intrusion, master_link, visitors
    from app.reid import reid_db

    now = now if now is not None else time.time()
    t0 = _day_start(now)
    cams = {c["id"]: c for c in camera_db.list_cameras()}
    cam = lambda cid: (cams.get(cid) or {}).get("name") or f"Camera {cid}"  # noqa: E731
    out: list[dict] = []

    # Employee zones: desk zones.
    desk_today = _safe(lambda: _desk_activity(t0), {})
    for z in _safe(desk_db.list_zones, []):
        a = desk_today.get(z["id"], {})
        out.append({"key": f"desk-{z['id']}", "id": z["id"], "type": "employee", "source": "desk", "name": z["zone_label"],
                    "camera_id": z["camera_id"], "camera": cam(z["camera_id"]), "shape": "polygon", "points": z["polygon"],
                    "enabled": True, "rule": "Records which recognised employee sits here and for how long (desk analytics).",
                    "activity": {"label": "desk sessions today", "count": a.get("n", 0), "people": a.get("people", 0), "last_at": a.get("last")}})

    # Visitor zones: Visitor Management seating areas.
    v_today = _safe(lambda: _visitor_activity(t0), {})
    for z in _safe(visitors.list_zones, []):
        a = v_today.get(z["camera_id"], {})
        out.append({"key": f"visitor-{z['camera_id']}", "id": z["camera_id"], "type": "visitor", "source": "visitor_area",
                    "name": f"Visitor seating · {cam(z['camera_id'])}", "camera_id": z["camera_id"], "camera": cam(z["camera_id"]),
                    "shape": "polygon", "points": z["polygon"], "enabled": z["enabled"],
                    "rule": (f"Unknown people seated here for {visitors.DETECTION_GAP_SECONDS:g} s raise a visitor alert with a clip, "
                             f"again every {visitors.REPEAT_ALERT_MINUTES:g} min while they stay. Recognised employees never do."),
                    "activity": {"label": "visitor alerts today", "count": a.get("alerts", 0), "people": a.get("visits", 0),
                                 "employees": a.get("employees", 0), "last_at": a.get("last")}})

    # Restricted / loitering / entry-gate zones (the intrusion engine), only when licensed.
    if master_link.feature_allowed("intrusion_detection"):
        st = _safe(intrusion.stats, {"per_zone_today": {}, "last_seen": {}})
        alerts_today = _safe(lambda: _intrusion_alerts(t0), {})
        for z in _safe(intrusion.list_zones, []):
            hours = f"{z['active_from']}–{z['active_to']}" if z.get("active_from") else "around the clock"
            if z["zone_type"] == "loitering":
                rule = f"Alerts when someone stays inside for {intrusion.format_duration(z.get('dwell_seconds') or 0)} ({hours})."
            else:
                rule = f"Alerts when someone enters{' this entry gate' if z['is_entry_gate'] else ''} ({hours})."
            if not z["alerts_enabled"]:
                rule += " Alerts are off; detections are still logged."
            out.append({"key": f"intrusion-{z['id']}", "id": z["id"], "type": "restricted", "source": "intrusion",
                        "name": f"{z['area']} · {z['name']}" if z["area"] else z["name"], "zone_name": z["name"], "area": z["area"],
                        "is_entry_gate": z["is_entry_gate"], "zone_type": z["zone_type"],
                        "camera_id": z["camera_id"], "camera": cam(z["camera_id"]), "shape": "polygon", "points": z["polygon"],
                        "enabled": z["enabled"], "rule": rule,
                        "activity": {"label": "detections today", "count": st["per_zone_today"].get(z["id"], 0),
                                     "alerts": alerts_today.get(z["id"], 0), "last_at": st["last_seen"].get(z["id"])}})

    # Entry / exit: the main gate line (Staff Count).
    gate_today = _safe(lambda: _gate_activity(t0), {})
    for g in _safe(_staff_gates, []):
        a = gate_today.get(g["camera_id"], {})
        out.append({"key": f"gate-{g['camera_id']}", "id": g["camera_id"], "type": "entry_exit", "source": "staff_gate",
                    "name": f"Main gate (staff entry/exit) · {cam(g['camera_id'])}", "camera_id": g["camera_id"], "camera": cam(g["camera_id"]),
                    "shape": "line", "points": g["line"], "enabled": g["enabled"],
                    "rule": "Employees crossing this line inwards are checked in, outwards checked out (attendance times).",
                    "activity": {"label": "crossings today", "count": a.get("entries", 0) + a.get("exits", 0),
                                 "entries": a.get("entries", 0), "exits": a.get("exits", 0), "last_at": a.get("last")}})

    # Entry / exit: footfall gates.
    ff_today = _safe(lambda: _footfall_activity(t0), {})
    from app.footfall import is_gate

    for c in _safe(reid_db.list_camera_configs, []):
        # The same rule footfall counts by: an enabled config on an Entry/Exit camera.
        if not c["enabled"] or not is_gate(cams.get(c["camera_id"]) or {}):
            continue
        a = ff_today.get(c["camera_id"], {})
        out.append({"key": f"footfall-{c['camera_id']}", "id": c["camera_id"], "type": "entry_exit", "source": "footfall_gate",
                    "name": f"Footfall gate · {cam(c['camera_id'])}", "camera_id": c["camera_id"], "camera": cam(c["camera_id"]),
                    "shape": "line" if c.get("entry_line") else "polygon", "points": c.get("entry_line") or c.get("roi"),
                    "enabled": True,
                    "rule": ("Counts each person entering through the line once a day (footfall)." if c.get("entry_line")
                             else "No entry line yet: counts everyone seen in its area. Draw a line to count only inward entries."),
                    "activity": {"label": "people counted today", "count": a.get("entries", 0), "exits": a.get("exits", 0), "last_at": a.get("last")}})

    counts = {t: sum(1 for z in out if z["type"] == t) for t in TYPES}
    return {"zones": out, "types": TYPES, "counts": counts}


def _desk_activity(t0: float) -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT zone_id, COUNT(*) AS n, COUNT(DISTINCT employee_id) AS people, MAX(end_ts) AS last "
                            "FROM desk_sessions WHERE start_ts >= ? GROUP BY zone_id", (t0,)).fetchall()
    return {r["zone_id"]: dict(r) for r in rows}


def _visitor_activity(t0: float) -> dict:
    out: dict = {}
    with _conn() as conn:
        for r in conn.execute("SELECT camera_id, COUNT(*) AS alerts, COUNT(DISTINCT COALESCE(visit_key, event_id)) AS visits, MAX(alert_at) AS last "
                              "FROM visitor_report_log WHERE alert_at >= ? GROUP BY camera_id", (t0,)):
            out[r["camera_id"]] = dict(r)
        for r in conn.execute("SELECT camera_id, COUNT(DISTINCT employee_id) AS employees FROM visitor_area_employee_log WHERE entered_at >= ? GROUP BY camera_id", (t0,)):
            out.setdefault(r["camera_id"], {})["employees"] = r["employees"]
    return out


def _intrusion_alerts(t0: float) -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT zone_id, COUNT(*) FROM alerts WHERE ts >= ? AND zone_id IS NOT NULL "
                            "AND event IN ('Intrusion Detected', 'Loitering Detected') GROUP BY zone_id", (t0,)).fetchall()
    return {r[0]: r[1] for r in rows}


def _staff_gates() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT camera_id, enabled, line FROM staff_camera_config WHERE line IS NOT NULL").fetchall()
    return [{"camera_id": r["camera_id"], "enabled": bool(r["enabled"]), "line": json.loads(r["line"])} for r in rows]


def _gate_activity(t0: float) -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT camera_id, SUM(event_type = 'ENTRY') AS entries, SUM(event_type = 'EXIT') AS exits, MAX(ts) AS last "
                            "FROM staff_events WHERE ts >= ? AND event_type IN ('ENTRY', 'EXIT') GROUP BY camera_id", (t0,)).fetchall()
    return {r["camera_id"]: {"entries": r["entries"] or 0, "exits": r["exits"] or 0, "last": r["last"]} for r in rows}


def _footfall_activity(t0: float) -> dict:
    from app.reid import reid_db

    with reid_db.get_connection() as conn:
        rows = conn.execute("SELECT camera_id, COUNT(DISTINCT person_id), SUM(event_type = ?), MAX(ts) FROM reid_events WHERE ts >= ? GROUP BY camera_id",
                            (reid_db.EVENT_EXIT, t0)).fetchall()
    return {r[0]: {"entries": r[1], "exits": r[2] or 0, "last": r[3]} for r in rows}
