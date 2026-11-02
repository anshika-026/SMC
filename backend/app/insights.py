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
import logging
import sqlite3
import time

from app import attendance, camera_db, camera_stream, employee_directory
from app import analytics_settings, config, db, master_link

log = logging.getLogger("insights")

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

    # Only report on analytics this deployment runs (config.ENABLED_FEATURES)
    # and the Master Portal license includes (always, in standalone mode).
    face_on = analytics_settings.available("face_recognition") and master_link.feature_allowed("face_recognition")
    footfall_on = analytics_settings.available("footfall") and master_link.feature_allowed("footfall_analytics")

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


def _clock(ts: float | None) -> str | None:
    return datetime.datetime.fromtimestamp(ts).strftime("%I:%M %p").lstrip("0") if ts else None


def _delta(now_v, prev_v):
    """Today vs the previous day, only when both are known."""
    if now_v is None or prev_v is None:
        return None
    return {"previous": prev_v, "change": now_v - prev_v}


def _safe(fn, default=None):
    try:
        return fn()
    except (sqlite3.Error, KeyError, ValueError):
        log.exception("dashboard: a section could not be built")
        return default


# Alerts that are about security and safety (cameras going offline leave a blind spot).
SECURITY_EVENTS = {"Unknown Person", "Person", "Restricted area entered", "Camera Offline",
                   "Intrusion Detected", "Loitering Detected"}
INTRUSION_EVENTS = ("Intrusion Detected", "Loitering Detected")


def business_overview(day: str | None = None, now: float | None = None) -> dict:
    """The business dashboard for one local date (default today): four areas -
    workforce, visitors, footfall & staffing, security - each built from the
    same functions its feature page uses, so the numbers always agree with
    those pages. Live figures (inside now, waiting now, staffing) exist only
    for today and are None for another date. A feature this deployment doesn't
    run is None, never zero."""
    from app import alerts, footfall, presence, visitor_registry, visitors, workforce
    from app.reid import reid_db
    from app.staff import service as staff_service

    now = now if now is not None else time.time()
    today = _iso(datetime.date.fromtimestamp(now))
    day = day or today
    is_today = day == today
    day_date = datetime.date.fromisoformat(day)
    prev_day = _iso(day_date - datetime.timedelta(days=1))
    day_start = datetime.datetime.combine(day_date, datetime.time())
    day_end = (day_start + datetime.timedelta(days=1)).timestamp()
    # "now" for a past date is the end of that day, so its numbers are complete.
    at = now if is_today else day_end - 1

    # Offered by this deployment AND included in the Master Portal license
    # (always true in standalone mode); a section that isn't is None.
    face_on = analytics_settings.available("face_recognition") and master_link.feature_allowed("attendance")
    footfall_on = analytics_settings.available("footfall") and master_link.feature_allowed("footfall_analytics")
    visitors_on = analytics_settings.available("visitors") and master_link.feature_allowed("visitor_management")
    staff_on = analytics_settings.available("staff_count") and master_link.feature_allowed("attendance")

    # ---- 1. workforce (the Attendance page's classification) --------------------------
    workforce_block = None
    if face_on:
        ov = workforce.overview(day, now=at)
        rows = ov["rows"]
        worked = [r for r in rows if r["status"] in workforce.WORKED]
        prev_rows = _safe(lambda: workforce.overview(prev_day, now=at)["rows"], [])
        prev_present = sum(1 for r in prev_rows if r["status"] in workforce.WORKED) if prev_rows else None
        total = len(rows)
        exceptions = []
        for r in rows:
            reasons = []
            if r["late"]:
                reasons.append(f"late (in {_clock(r['first_in'])})")
            if r["half_day"]:
                reasons.append("half day")
            if r["status"] == workforce.ABSENT:
                reasons.append("absent")
            if r["missing_entry"]:
                reasons.append("seen by face, no gate entry")
            if r["missing_exit"]:
                reasons.append("exit not confirmed")
            if r["long_outside"]:
                reasons.append("long time outside")
            if r["over_late_limit"]:
                reasons.append(f"late {r['month']['late']}x this month")
            if reasons:
                exceptions.append({"employee_id": r["employee_id"], "name": r["name"], "status": r["status"], "reasons": reasons})
        split = _safe(enrollment_split, {"enrolled": 0, "not_enrolled": 0})
        workforce_block = {
            "present": len(worked), "total": total,
            "absent": sum(1 for r in rows if r["status"] == workforce.ABSENT),
            "late": sum(1 for r in rows if r["late"]),
            "half_day": sum(1 for r in rows if r["half_day"]),
            "on_leave": sum(1 for r in rows if r["status"] in (workforce.ON_LEAVE, workforce.WFH)),
            "attendance_pct": round(len(worked) / total * 100) if total else 0,
            "vs_previous": _delta(len(worked), prev_present),
            "repeat_late": [{"name": r["name"], "count": r["month"]["late"]} for r in rows if r["over_late_limit"]],
            "late_allowance": ov["policy"]["late_allowance_per_month"],
            "exceptions": exceptions,
            "employees": [{"employee_id": r["employee_id"], "name": r["name"], "status": r["status"], "first_in": r["first_in"],
                           "last_out": r["last_out"], "late": r["late"], "location": r.get("location_detail")} for r in rows],
            "recognition": {"enrolled": split["enrolled"], "not_enrolled": split["not_enrolled"],
                            "recognised_today": sum(1 for r in rows if r["first_in"]),
                            "unknown_faces": unknown_faces_today(at) if is_today else None},
        }

    # ---- 2. visitors (Visitor Management's visit records) ----------------------------
    visitor_block = None
    if visitors_on:
        k = visitor_registry.kpis(day, now=at)
        kp = _safe(lambda: visitor_registry.kpis(prev_day, now=at))
        threshold = visitor_registry.get_settings()["waiting_alert_minutes"]
        waiting = visitor_registry.waiting_now(now) if is_today else []
        visits = visitor_registry.list_visits(date_from=day, date_to=day, limit=500, now=at)
        area = _safe(lambda: visitors.kpis(now=at, day=day), {})
        visitor_block = {
            "visitors": k["visitors_today"], "first_time": k["first_time_today"], "returning": k["repeat_today"],
            "vs_previous": _delta(k["visitors_today"], kp["visitors_today"] if kp else None),
            "inside_now": k["inside_now"] if is_today else None,
            "waiting_now": len(waiting) if is_today else None,
            "waiting": [{**w, "over": w["waiting_seconds"] >= threshold * 60} for w in waiting],
            "waiting_threshold_minutes": threshold,
            "average_waiting_seconds": k["average_waiting_seconds"], "waiting_measured": k["waiting_measured"],
            "average_duration_seconds": k["average_duration_seconds"],
            "checked_out": k["checked_out_today"], "pending": k["pending_today"],
            "visits": [{"id": v["id"], "visitor_name": v["visitor_name"], "visitor_id": v["visitor_id"], "host_name": v["host_name"],
                        "purpose": v["purpose"], "status": v["status"], "entry_at": v["entry_at"], "exit_at": v["exit_at"],
                        "meeting_start_at": v["meeting_start_at"], "waiting_seconds": v["waiting_seconds"],
                        "duration_seconds": v["duration_seconds"], "elapsed_seconds": v["elapsed_seconds"],
                        "first_visit": v["first_visit"]} for v in visits],
            "seating_area_alerts": area.get("alerts"),
        }

    # ---- 3. footfall & staffing ---------------------------------------------------------
    footfall_block = None
    if footfall_on:
        f = footfall.service.day_stats(day)
        emp_hourly = _safe(lambda: _employee_entries_by_hour(day), {})
        hourly = []
        for h in f["hourly"]:
            e = min(emp_hourly.get(h["hour"], 0), h["entries"])
            hourly.append({"hour": h["hour"], "label": _hour_label(h["hour"]), "entries": h["entries"], "employees": e, "visitors": h["entries"] - e})
        trend = _safe(lambda: reid_db.footfall_daily(_iso(day_date - datetime.timedelta(days=6)), day), [])
        footfall_block = {
            "entries": f["entries"], "exits": f["exits"], "employee_entries": f["employee_entries"], "visitor_entries": f["visitor_entries"],
            "vs_previous": _delta(f["entries"], f["previous_day_entries"]),
            "peak_hour": f["peak_hour"], "peak_label": _hour_label(f["peak_hour"]), "hourly": hourly,
            "trend": [{"day": d["day"], "entries": d["entries"], "employees": d["employee_entries"],
                       "visitors": d["entries"] - d["employee_entries"]} for d in trend],
            "gates": [{"name": g["name"], "today": g["unique_today"], "entry_line": bool(g.get("entry_line"))}
                      for g in footfall.service.summary()["gates"]] if is_today else None,
            "gates_without_entry_line": f["gates_without_entry_line"],
        }

    staff_block = None
    if staff_on and staff_service.service.manager is not None:
        c = staff_service.service.manager.counts(now=at)
        staff_block = {"inside_now": c["total_persons"] if is_today else None,
                       "employees_inside": c["known_staff"] if is_today else None,
                       "others_inside": c["unknown_persons"] if is_today else None,
                       "entries": c["total_entries_today"], "exits": c["total_exits_today"],
                       "peak_inside": c["peak_inside_today"], "peak_at": c["peak_at"]}

    areas, understaffed, staff_now = None, None, None
    if is_today:
        areas = _safe(lambda: presence.area_staffing(now), [])
        staff_now = {"count": _safe(lambda: presence.staff_seen_now(now), 0), "window_minutes": presence.SEEN_INSIDE_MINUTES}
        understaffed = [{"area": a["area"], "detail": f"{a['employees']} of {a['target']} required employees", "short_by": a["short_by"]}
                        for a in areas if a["short_by"]]
        if visitors_on:
            names = {c["id"]: c["name"] for c in camera_db.list_cameras()}
            for z in _safe(visitors.list_zones, []):
                if not z["enabled"]:
                    continue
                st = visitors.service.camera_status(z["camera_id"])
                if st["inside_now"] > 0 and st["employees_now"] == 0:
                    understaffed.append({"area": f"Visitor seating - {names.get(z['camera_id'], z['camera_id'])}",
                                         "detail": f"{st['inside_now']} visitor(s) seated, no employee with them", "short_by": None})

    # ---- 4. security & safety -------------------------------------------------------------
    # Only security / safety alerts: attendance (late arrival) and operations
    # (footfall stopped) alerts belong to their own areas, not here.
    sec = lambda a: a["event"] in SECURITY_EVENTS  # noqa: E731
    raised = [a for a in alerts.list_alerts("today", limit=1000, now=day_start.timestamp() + 1) if a["ts"] < day_end and sec(a)]
    prev_raised = _safe(lambda: [a for a in alerts.list_alerts("yesterday", limit=1000, now=day_start.timestamp() + 1) if sec(a)], [])
    open_alerts = [a for a in raised if a["status"] != "Resolved"]
    unknown = [a for a in raised if a["event"] in ("Unknown Person", "Person")]
    # Restricted-area alerts: the intrusion engine's ("Intrusion Detected"), plus any left from
    # the earlier SMC restricted-zone monitor ("Restricted area entered").
    restricted = [a for a in raised if a["event"] in ("Restricted area entered", "Intrusion Detected")]
    loitering = [a for a in raised if a["event"] == "Loitering Detected"]
    intrusion_on = master_link.feature_allowed("intrusion_detection") and analytics_settings.available("intrusion")
    restricted_entries = _safe(lambda: _restricted_entries(day_start.timestamp(), day_end), None)
    seating = _safe(lambda: [e for e in visitors.list_events(limit=200) if day_start.timestamp() <= (e["alert_at"] or e["entered_at"]) < day_end], [])
    sev = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3}
    incidents = [{"kind": "alert", "id": a["id"], "event": "Person" if a["event"] == "Unknown Person" else a["event"],
                  "severity": a["severity"], "camera": a["camera_name"], "ts": a["ts"], "time": _clock(a["ts"]),
                  "status": a["status"], "message": a["message"], "picture": a["has_snapshot"],
                  # Intrusion / loitering alerts carry an evidence clip (evidence.py).
                  "alert_clip": a.get("has_clip", False), "zone": a.get("zone_name")} for a in raised]
    incidents += [{"kind": "visitor_area", "id": e["id"], "event": "Unknown person in visitor seating", "severity": "Medium",
                   "camera": e["camera_name"], "ts": e["alert_at"] or e["entered_at"], "time": _clock(e["alert_at"] or e["entered_at"]),
                   "status": "Left" if e["status"] == "left" else "In area", "message": "", "clip": e["clip_ready"]} for e in seating]
    incidents.sort(key=lambda x: -x["ts"])
    security_block = {
        "active": len(open_alerts),
        "high_or_critical": sum(1 for a in open_alerts if a["severity"] in ("Critical", "High")),
        "raised": len(raised), "vs_previous": _delta(len(raised), len(prev_raised) if prev_raised is not None else None),
        "unknown_persons": len(unknown) + len(seating),
        "restricted_violations": len(restricted),
        "restricted_entries": restricted_entries,
        # Intrusion / loitering alerts (intrusion.py). None (unavailable, never zero) when
        # the feature isn't offered or licensed here.
        "intrusion": len(restricted) + len(loitering) if intrusion_on else None,
        "loitering": len(loitering) if intrusion_on else None,
        "intrusion_open": sum(1 for a in restricted + loitering if a["status"] != "Resolved") if intrusion_on else None,
        "intrusion_with_clip": sum(1 for a in restricted + loitering if a.get("has_clip")) if intrusion_on else None,
        "open": [{"id": a["id"], "event": "Person" if a["event"] == "Unknown Person" else a["event"], "severity": a["severity"],
                  "camera": a["camera_name"], "time": _clock(a["ts"]), "status": a["status"], "message": a["message"]}
                 for a in sorted(open_alerts, key=lambda a: (sev.get(a["severity"], 9), -a["ts"]))[:10]],
        "incidents": incidents[:25],
    }

    # ---- attention required -------------------------------------------------------------
    attention = []
    for u in understaffed or []:
        attention.append({"kind": "staffing", "tone": "bad", "label": f"Understaffed: {u['area']}", "detail": u["detail"], "link": "/attendance"})
    if workforce_block and workforce_block["repeat_late"]:
        rl = workforce_block["repeat_late"]
        attention.append({"kind": "late", "tone": "warn", "link": "/attendance",
                          "label": f"{len(rl)} employee{'s' if len(rl) != 1 else ''} late more than {workforce_block['late_allowance']}x this month",
                          "detail": ", ".join(f"{x['name']} ({x['count']})" for x in rl[:5]) + (" …" if len(rl) > 5 else "")})
    if visitor_block:
        over = [w for w in visitor_block["waiting"] if w["over"]]
        if over:
            attention.append({"kind": "waiting", "tone": "warn", "link": "/visitors",
                              "label": f"{len(over)} visitor{'s' if len(over) != 1 else ''} waiting over {visitor_block['waiting_threshold_minutes']} min",
                              "detail": ", ".join(f"{w['visitor_name']} for {w['host_name']} ({round(w['waiting_seconds'] / 60)} min)" for w in over[:4])})
    if security_block["active"]:
        top = security_block["open"][0]
        attention.append({"kind": "alerts", "tone": "bad" if security_block["high_or_critical"] else "warn", "link": "/alerts",
                          "label": f"{security_block['active']} unresolved security alert{'s' if security_block['active'] != 1 else ''}"
                                   + (f" ({security_block['high_or_critical']} high or critical)" if security_block["high_or_critical"] else ""),
                          "detail": f"Latest top priority: {top['event']} on {top['camera']} at {top['time']}"})
    if is_today:
        for c in _safe(lambda: camera_health(now), []):
            if c["state"] == "offline":
                attention.append({"kind": "camera", "tone": "bad", "link": "/cameras", "label": f"{c['name']} is offline",
                                  "detail": "No video from this camera"})

    # ---- the day in sentences -------------------------------------------------------------
    summary = []
    if workforce_block:
        w = workforce_block
        text = f"{w['present']} of {w['total']} employees came in ({w['attendance_pct']}%): {w['absent']} absent, {w['late']} late"
        if w["vs_previous"]:
            d = w["vs_previous"]["change"]
            text += f"; {abs(d)} {'more' if d >= 0 else 'fewer'} than the day before" if d else "; same as the day before"
        summary.append({"area": "Workforce", "tone": "warn" if w["absent"] or w["late"] else "ok", "text": text + ".", "link": "/attendance"})
    if visitor_block:
        v = visitor_block
        if v["visitors"]:
            text = f"{v['visitors']} visitor{'s' if v['visitors'] != 1 else ''} ({v['returning']} returning)"
            if v["average_waiting_seconds"] is not None:
                text += f", average wait {round(v['average_waiting_seconds'] / 60)} min"
            if v["inside_now"]:
                text += f", {v['inside_now']} inside now"
        else:
            text = "No visitors registered"
        summary.append({"area": "Visitors", "tone": "warn" if any(x["over"] for x in v["waiting"]) else "ok", "text": text + ".", "link": "/visitors"})
    if footfall_block:
        f = footfall_block
        text = (f"{f['entries']} {'person' if f['entries'] == 1 else 'people'} entered "
                f"({f['employee_entries']} employee{'s' if f['employee_entries'] != 1 else ''}, {f['visitor_entries']} other{'s' if f['visitor_entries'] != 1 else ''})")
        if f["peak_label"]:
            text += f", busiest {f['peak_label']}"
        if f["vs_previous"] and f["vs_previous"]["previous"]:
            pct = round(f["vs_previous"]["change"] / f["vs_previous"]["previous"] * 100)
            text += f" ({'+' if pct >= 0 else ''}{pct}% vs the day before)"
        summary.append({"area": "Footfall", "tone": "ok", "text": text + ".", "link": "/visitors?tab=footfall"})
    if understaffed is not None:
        summary.append({"area": "Staffing", "tone": "bad" if understaffed else "ok", "link": "/attendance",
                        "text": "; ".join(f"{u['area']}: {u['detail']}" for u in understaffed) + "." if understaffed
                        else ("All areas with a minimum have enough employees right now." if any(a["target"] for a in areas or [])
                              else "No minimum staffing set for any area yet.")})
    s = security_block
    summary.append({"area": "Security", "link": "/alerts", "tone": "bad" if s["high_or_critical"] else ("warn" if s["active"] else "ok"),
                    "text": (f"{s['raised']} alert{'s' if s['raised'] != 1 else ''} raised, {s['active']} still open; "
                             f"{s['unknown_persons']} unknown-person detection{'s' if s['unknown_persons'] != 1 else ''}, "
                             f"{s['restricted_violations']} restricted-area violation{'s' if s['restricted_violations'] != 1 else ''}.")})

    return {
        "date": day, "is_today": is_today,
        "workforce": workforce_block, "visitors": visitor_block, "footfall": footfall_block, "staff": staff_block,
        "areas": areas, "understaffed": understaffed, "staff_now": staff_now, "security": security_block,
        "attention": attention, "summary": summary,
    }


def _employee_entries_by_hour(day: str) -> dict[int, int]:
    from app.reid import reid_db

    with reid_db.get_connection() as conn:
        rows = conn.execute("SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER), COUNT(*) FROM footfall_clips "
                            "WHERE day = ? AND employee_id IS NOT NULL GROUP BY 1", (day,)).fetchall()
    return {h: n for h, n in rows}


def _restricted_entries(t0: float, t1: float) -> int | None:
    with _conn() as conn:
        try:
            # Itemized intrusion detections (intrusion.py logs one per zone at most every 30 s).
            return conn.execute("SELECT COUNT(*) FROM intrusion_events WHERE ts >= ? AND ts < ?", (t0, t1)).fetchone()[0]
        except sqlite3.OperationalError:
            return None
