"""
insights.py

Summaries for the Dashboard and the Workforce Insights overview, built only
from data the backend already records: attendance (attendance.py), unique
footfall (footfall.py), desk analytics (desks.py), live camera streams
(camera_stream.py), unrecognised faces (face_db's face_pending queue) and
face-training labels. Nothing here is estimated or invented: a number with
no data behind it comes back as None and the page says so.
"""

import datetime
import sqlite3
import time

from app import attendance, camera_db, camera_stream, employee_directory
from app import analytics_settings, config, db

DB_PATH = config.DB_PATH

# A streaming camera that hasn't delivered a frame for this long is offline.
OFFLINE_AFTER_SECONDS = 30


def _conn():
    """Shared connection policy (app/db.py): lock timeout + busy_timeout,
    commit on success, rollback on error, and always closed."""
    return db.connect(DB_PATH, row_factory=sqlite3.Row)


def _iso(d: datetime.date) -> str:
    return d.isoformat()


def camera_health(now: float | None = None) -> list[dict]:
    """Per camera: "online" (frame in the last 30 s), "offline" (streaming but
    silent), or "idle" (not streaming: nobody watching and no analytics on it)."""
    now = now if now is not None else time.time()
    out = []
    for c in camera_db.list_cameras():
        last = camera_stream.get_stream(c["id"]).last_frame_at
        if not camera_db.is_streamable(c):
            state = "disabled"
        elif last is None:
            state = "idle"
        elif now - last <= OFFLINE_AFTER_SECONDS:
            state = "online"
        else:
            state = "offline"
        out.append({"id": c["id"], "name": c["name"], "state": state, "last_frame_at": last,
                    "face_recognition": bool(c.get("attendance_tracking"))})
    return out


def unknown_faces_today(now: float | None = None) -> int:
    """Faces captured today that face recognition couldn't confidently name."""
    now = now if now is not None else time.time()
    start = datetime.datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    try:
        with _conn() as conn:
            return conn.execute("SELECT COUNT(*) FROM face_pending WHERE captured_at >= ?", (start,)).fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def enrollment_split() -> dict:
    """Employees with at least one labelled face-training photo (recognisable)
    vs none (the model can't recognise them yet)."""
    try:
        with _conn() as conn:
            labelled = {r[0] for r in conn.execute(
                "SELECT DISTINCT employee_id FROM face_training_captures WHERE label_status = 'labeled' AND employee_id IS NOT NULL")}
    except sqlite3.OperationalError:
        labelled = set()
    enrolled = sum(1 for emp in employee_directory.EMPLOYEES if emp in labelled)
    return {"enrolled": enrolled, "not_enrolled": len(employee_directory.EMPLOYEES) - enrolled}


def workforce_overview(date: str | None = None, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    day = datetime.date.fromisoformat(date) if date else datetime.date.fromtimestamp(now)
    today = attendance.day_report(_iso(day), now=now)
    yesterday = attendance.day_report(_iso(day - datetime.timedelta(days=1)), now=now)
    s = today["stats"]
    exited = sum(1 for r in today["rows"] if r["status"] == "Present")  # seen today, not in the last 15 min
    trend = []
    for i in range(6, -1, -1):
        d = day - datetime.timedelta(days=i)
        rep = today if i == 0 else attendance.day_report(_iso(d), now=now)
        trend.append({"date": _iso(d), "name": d.strftime("%a"), "value": rep["stats"]["present"]})
    return {
        "date": _iso(day),
        "total_employees": s["total"],
        "present": s["present"],
        "on_site": sum(1 for r in today["rows"] if r["status"] == "On site"),
        "exited": exited,
        "not_detected": s["absent"],
        "on_leave": s["on_leave"],
        "late": s["late"],
        "attendance_pct": s["attendance_pct"],
        "attendance_pct_yesterday": yesterday["stats"]["attendance_pct"],
        "present_yesterday": yesterday["stats"]["present"],
        "trend": trend,
        "enrollment": enrollment_split(),
    }


def dashboard_summary(now: float | None = None) -> dict:
    from app import desks, footfall

    now = now if now is not None else time.time()
    day = datetime.date.fromtimestamp(now)
    att = attendance.day_report(_iso(day), now=now)
    att_y = attendance.day_report(_iso(day - datetime.timedelta(days=1)), now=now)
    cams = camera_health(now)
    ff = footfall.service.summary()
    desk_rows = desks.service.report(_iso(day))["employees"] if desks.service.tracker else []

    # Only report on analytics this deployment runs (config.ENABLED_FEATURES).
    face_on = analytics_settings.available("face_recognition")
    footfall_on = analytics_settings.available("footfall")

    attention = []
    for c in cams:
        if c["state"] == "offline":
            ago = int((now - c["last_frame_at"]) // 60)
            attention.append({"label": f"{c['name']} is offline", "detail": f"No video for {ago} min" if ago else "No video for under a minute", "tag": "Critical"})
    for g in ff["gates"] if footfall_on else []:
        if not g["counting"]:
            attention.append({"label": f"Footfall stopped at {g['name']}", "detail": "Counting failed on this gate; check the backend log", "tag": "Critical"})
        elif not g.get("zone"):
            attention.append({"label": f"No counting zone at {g['name']}", "detail": "Footfall counts the whole view, including seating", "tag": "Warning"})
    late = [r for r in att["rows"] if r["arrival"] == "Late arrival"] if face_on else []
    if late:
        names = ", ".join(r["name"] for r in late[:3]) + (f" and {len(late) - 3} more" if len(late) > 3 else "")
        attention.append({"label": f"{len(late)} late arrival{'s' if len(late) != 1 else ''}", "detail": names, "tag": "Warning"})

    insights = []
    s = att["stats"]
    if face_on and s["present"]:
        insights.append(f"{s['present']} of {s['total']} employees ({s['attendance_pct']}%) have been seen today; "
                        f"yesterday it was {att_y['stats']['present']}.")
    if footfall_on and ff["busiest_hour"] is not None and ff["unique_today"]:
        h = ff["busiest_hour"]
        insights.append(f"Footfall is busiest at {datetime.time(h).strftime('%I %p').lstrip('0')} so far, "
                        f"with {ff['unique_today']} unique visitors across {len(ff['gates'])} gate{'s' if len(ff['gates']) != 1 else ''}.")
    if desk_rows and analytics_settings.enabled("desk_analytics"):
        top = desk_rows[0]
        insights.append(f"{top['name']} has the most desk time today: {top['desk_seconds'] // 3600}h {(top['desk_seconds'] % 3600) // 60:02d}m.")
    unknown = unknown_faces_today(now) if face_on else 0
    if unknown:
        insights.append(f"{unknown} face{'s' if unknown != 1 else ''} today couldn't be matched to an employee; "
                        "label them on the Face Training page to improve recognition.")
    if not insights:
        insights.append("No activity recorded yet today.")

    return {
        "people_present": {"value": s["present"], "of": s["total"], "yesterday": att_y["stats"]["present"]},
        "unknown_faces": unknown,
        "cameras": {"online": sum(c["state"] == "online" for c in cams), "total": len(cams),
                    "disabled": sum(c["state"] == "disabled" for c in cams),
                    "offline": sum(c["state"] == "offline" for c in cams), "list": cams},
        "needs_attention": attention,
        "insights": insights,
    }


def _hour_label(h: int | None) -> str | None:
    if h is None:
        return None
    a, b = datetime.time(h).strftime("%I %p").lstrip("0"), datetime.time((h + 1) % 24).strftime("%I %p").lstrip("0")
    return f"{a}-{b}"


def business_overview(day: str | None = None, now: float | None = None) -> dict:
    """The business dashboard for one local date (default today), built from
    the same functions the feature pages use, so the numbers always agree with
    them. Live figures (staff inside, visitors waiting, staffing gaps) exist
    only for today; for another date they are None. A feature this deployment
    doesn't run is None, never zero."""
    from app import alerts, footfall, visitors
    from app.staff import service as staff_service

    now = now if now is not None else time.time()
    today = _iso(datetime.date.fromtimestamp(now))
    day = day or today
    is_today = day == today
    day_start = datetime.datetime.strptime(day, "%Y-%m-%d")
    # "now" for a past date is the end of that day, so its numbers are complete.
    at = now if is_today else (day_start + datetime.timedelta(days=1)).timestamp() - 1

    face_on = analytics_settings.available("face_recognition")
    footfall_on = analytics_settings.available("footfall")
    visitors_on = analytics_settings.available("visitors")
    staff_on = analytics_settings.available("staff_count")

    # ---- attendance --------------------------------------------------------------
    attendance_block = None
    if face_on:
        rep = attendance.day_report(day, now=at)
        s = rep["stats"]
        names = {r["employee_id"]: r["name"] for r in rep["rows"]}
        exceptions = []
        for r in rep["rows"]:
            reasons = []
            if r["arrival"] == "Late arrival":
                reasons.append(f"late (in {r['time_in']})")
            if r["departure"] == "Early departure":
                reasons.append(f"left early (last seen {r['time_out']})")
            if r["employee_id"] in rep["late_repeat_ids"]:
                reasons.append(f"late {s['late_repeat_threshold']}+ times in 30 days")
            if r["status"] in ("Present", "On site") and r["time_in"] == r["time_out"]:
                reasons.append("seen only once (no time-out)")
            if reasons:
                exceptions.append({"employee_id": r["employee_id"], "name": r["name"], "reasons": reasons})
        attendance_block = {
            **{k: s[k] for k in ("present", "total", "absent", "on_leave", "late", "attendance_pct", "late_repeat",
                                 "late_repeat_threshold", "seen_once", "early_departures", "overtime", "overtime_minutes",
                                 "shift_end")},
            "late_after": rep["late_after"],
            "late_repeat_names": [names.get(e, e) for e in rep["late_repeat_ids"]],
            "exceptions": exceptions,
            # Where each employee present that day was last seen (by face recognition).
            "presence": [{"name": r["name"], "camera": r["camera"], "last_seen": r["last_seen"], "status": r["status"]}
                         for r in rep["rows"] if r["status"] in ("Present", "On site")],
        }

    # ---- visitors --------------------------------------------------------------
    visitor_block = None
    if visitors_on:
        k = visitors.kpis(now=now, day=day)
        visitor_block = {
            "visitors": k["visitors_today"],
            "average_wait_seconds": k["average_stay_seconds"],
            "over_threshold": k["long_stays"],
            "threshold_minutes": k["long_stay_minutes"],
            "waiting_now": k["waiting_now"] if is_today else None,
            "alerts": k["alerts"],
            "employees_seen": k["employees_seen_today"],
            "hourly": k["hourly"],
            "peak_hour": k["peak_hour"],
            "peak_label": _hour_label(k["peak_hour"]),
            # Visitors aren't identified (no face match for non-employees), so a
            # returning visitor can't be told from a new one here.
            "repeat_visitors": None,
        }

    # ---- footfall --------------------------------------------------------------
    footfall_block = None
    if footfall_on:
        f = footfall.service.day_stats(day)
        prev = f["previous_day_entries"]
        footfall_block = {**f, "peak_label": _hour_label(f["peak_hour"]),
                          "change_pct": round((f["entries"] - prev) / prev * 100) if prev else None}

    # ---- staff and zones ---------------------------------------------------------
    staff_block = None
    if staff_on and staff_service.service.manager is not None:
        c = staff_service.service.manager.counts(now=at)
        staff_block = {
            "inside_now": c["total_persons"] if is_today else None,
            "employees_inside": c["known_staff"] if is_today else None,
            "others_inside": c["unknown_persons"] if is_today else None,
            "entries": c["total_entries_today"],
            "exits": c["total_exits_today"],
            "peak_inside": c["peak_inside_today"],
            "peak_at": c["peak_at"],
        }

    zones, gaps = [], []
    if visitors_on and is_today:
        names = {c["id"]: c["name"] for c in camera_db.list_cameras()}
        for z in visitors.list_zones():
            if not z["enabled"]:
                continue
            st = visitors.service.camera_status(z["camera_id"])
            row = {"camera_id": z["camera_id"], "area": f"Visitor area - {names.get(z['camera_id'], z['camera_id'])}",
                   "visitors": st["inside_now"], "employees": st["employees_now"], "unverified": st["unverified_now"],
                   "online": st["online"]}
            zones.append(row)
            # A staffing gap we can actually see: visitors waiting, no employee there.
            if st["inside_now"] > 0 and st["employees_now"] == 0:
                gaps.append({"area": row["area"], "detail": f"{st['inside_now']} visitor(s) waiting, no employee in the area"})

    # ---- important events (alerts raised that day) ------------------------------------
    day_end = (day_start + datetime.timedelta(days=1)).timestamp()
    raised = [a for a in alerts.list_alerts("today", limit=500, now=day_start.timestamp() + 1) if a["ts"] < day_end]
    open_alerts = [a for a in raised if a["status"] != "Resolved"]
    sev = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    events = [{"id": a["id"], "event": "Person" if a["event"] == "Unknown Person" else a["event"], "severity": a["severity"],
               "camera": a["camera_name"], "time": datetime.datetime.fromtimestamp(a["ts"]).strftime("%I:%M %p").lstrip("0"),
               "status": a["status"], "message": a["message"]}
              for a in sorted(open_alerts, key=lambda a: (sev.get(a["severity"], 9), -a["ts"]))[:8]]

    # ---- the day in sentences -----------------------------------------------------
    summary = []
    if attendance_block:
        a = attendance_block
        line = f"{a['present']} of {a['total']} employees present ({a['attendance_pct']}%), {a['late']} late"
        if a["early_departures"]:
            line += f", {a['early_departures']} left early"
        if a["overtime"]:
            line += f", {a['overtime']} worked overtime"
        summary.append({"area": "Attendance", "text": line + ".", "tone": "warn" if a["exceptions"] else "ok",
                        "link": "/attendance"})
        if a["exceptions"]:
            summary.append({"area": "Attendance", "tone": "warn", "link": "/attendance",
                            "text": f"{len(a['exceptions'])} attendance exception{'s' if len(a['exceptions']) != 1 else ''}"
                                    + (f"; {a['late_repeat']} employee(s) late {a['late_repeat_threshold']}+ times this month." if a["late_repeat"] else ".")})
    if visitor_block:
        v = visitor_block
        if v["visitors"]:
            text = f"{v['visitors']} visitor{'s' if v['visitors'] != 1 else ''}, average wait {round(v['average_wait_seconds'] / 60)} min"
            if v["over_threshold"]:
                text += f"; {v['over_threshold']} waited over {v['threshold_minutes']:g} min"
            summary.append({"area": "Visitors", "text": text + ".", "tone": "warn" if v["over_threshold"] else "ok", "link": "/visitors"})
        else:
            summary.append({"area": "Visitors", "text": "No visitors recorded.", "tone": "ok", "link": "/visitors"})
    if footfall_block:
        f = footfall_block
        text = f"{f['entries']} inward entr{'ies' if f['entries'] != 1 else 'y'}"
        if f["change_pct"] is not None:
            text += f" ({'+' if f['change_pct'] >= 0 else ''}{f['change_pct']}% vs the day before)"
        if f["peak_label"]:
            text += f", busiest {f['peak_label']}"
        summary.append({"area": "Footfall", "text": text + ".", "tone": "ok", "link": "/footfall"})
        if f["gates_without_entry_line"]:
            summary.append({"area": "Footfall", "tone": "warn", "link": "/footfall",
                            "text": f"{f['gates_without_entry_line']} gate(s) have no entry line, so they also count people outside the gate."})
    if is_today:
        summary.append({"area": "Staffing", "link": "/visitors",
                        "text": "; ".join(g["detail"] + f" ({g['area']})" for g in gaps) + "." if gaps else "No staffing gaps in monitored areas right now.",
                        "tone": "bad" if gaps else "ok"})
    if open_alerts:
        crit = sum(1 for a in open_alerts if a["severity"] in ("Critical", "High"))
        summary.append({"area": "Alerts", "link": "/alerts", "tone": "bad" if crit else "warn",
                        "text": f"{len(open_alerts)} open alert{'s' if len(open_alerts) != 1 else ''}" + (f", {crit} high or critical." if crit else ".")})

    return {
        "date": day,
        "is_today": is_today,
        "attendance": attendance_block,
        "visitors": visitor_block,
        "footfall": footfall_block,
        "staff": staff_block,
        "zones": zones,
        "staffing_gaps": gaps if is_today else None,
        "events": events,
        "open_alerts": len(open_alerts),
        "summary": summary,
    }
