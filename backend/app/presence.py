"""
presence.py

Employee presence investigation for the Attendance page: where has this
employee actually been seen today, by which camera, how recently, and is that
evidence live or historical, verified or not.

Evidence (all recorded or live data; nothing is inferred from the attendance
status, and nothing here changes attendance - corrections are a separate,
explicit admin action, workforce.add_correction):

  live_face        the face pipeline is tracking a person on a camera RIGHT NOW
                   and has recognised them as this employee by FACE (verified)
  live_appearance  a person tracked right now whose identity comes only from
                   body appearance (matched to someone face-recognised earlier
                   today) - a lead, NOT a verified identity
  face             face recognition events today (camera, first / last seen,
                   number of sightings, best confidence)
  gate             Staff Count gate sessions identified as this employee
  footfall         a footfall gate entry the gate's face check named them in

Verdict:
  verified_live    recognised by face on a camera now
  verified_recent  recognised by face within SEEN_INSIDE_MINUTES (not the gate camera)
  seen_earlier     recognised earlier today, not recently
  unverified       only an appearance match, or nothing: "Presence not verified"

The Main Hall cameras (the two cameras covering where employees sit) are a
saved setting (presence_settings), chosen by the admin; nothing is hardcoded.

Camera locations (zones) come only from configuration: the Main Hall setting,
the Staff Count gate camera, the Visitor Management seating-area cameras, and
whatever an admin sets per camera (which wins). A camera with none of these is
"location not mapped" - never guessed from its name.

Recommendation (which camera to check this employee on), most reliable first:
  1. a camera recognising them by FACE right now (live, verified)
  2. the camera of their most recent face recognition (historical: they may
     have moved on), with the other cameras covering that location as
     alternatives
  3. a live appearance-only match (a lead, not an identity)
  4. nothing: "Camera location unavailable", the admin picks a camera
A recommended camera that is offline is swapped for an online one covering
the same location, and the reason says so.
"""

from __future__ import annotations

import datetime
import json
import sqlite3
import time

from app import audit, camera_db, config, db, employee_directory, workforce

DB_PATH = config.DB_PATH
SEEN_INSIDE_MINUTES = workforce.SEEN_INSIDE_MINUTES


def _conn():
    return db.connect(DB_PATH, row_factory=sqlite3.Row)


def init_db() -> None:
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS presence_settings (key TEXT PRIMARY KEY, value TEXT, updated_at REAL, updated_by TEXT)")


# --------------------------------------------------------------------------- Main Hall cameras

def _camera_public(cam: dict | None) -> dict | None:
    if not cam:
        return None
    return {"camera_id": cam["id"], "name": cam["name"], "site": cam.get("site"), "purpose": cam.get("purpose"),
            "protocol": cam.get("protocol"), "live_feed": bool(cam.get("live_feed_enabled", 1))}


def get_main_hall() -> dict:
    """{cameras: [Camera 01, Camera 02] (None where not chosen), configured}."""
    with _conn() as conn:
        r = conn.execute("SELECT value, updated_at, updated_by FROM presence_settings WHERE key = 'main_hall_cameras'").fetchone()
    ids = json.loads(r["value"]) if r and r["value"] else [None, None]
    ids = (ids + [None, None])[:2]
    cams = [_camera_public(camera_db.get_camera(int(i))) if i is not None else None for i in ids]
    return {"cameras": cams, "configured": all(cams), "updated_at": r["updated_at"] if r else None,
            "updated_by": r["updated_by"] if r else None}


def set_main_hall(camera_ids: list, actor: dict | None = None) -> dict:
    ids = list(camera_ids or [])
    if len(ids) != 2 or any(i in (None, "") for i in ids):
        raise ValueError("Choose two cameras: Camera 01 (left) and Camera 02 (right).")
    ids = [int(i) for i in ids]
    if ids[0] == ids[1]:
        raise ValueError("Choose two different cameras.")
    for i in ids:
        if camera_db.get_camera(i) is None:
            raise ValueError(f"Camera {i} not found.")
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO presence_settings (key, value, updated_at, updated_by) VALUES ('main_hall_cameras', ?, ?, ?)",
                     (json.dumps(ids), time.time(), audit._actor(actor)))
    audit.record("presence.main_hall_cameras", actor, None, camera_ids=ids)
    return get_main_hall()


# --------------------------------------------------------------------------- camera locations

STREAM_FRESH_SECONDS = 15


def _admin_zones() -> dict[int, str]:
    with _conn() as conn:
        r = conn.execute("SELECT value FROM presence_settings WHERE key = 'camera_zones'").fetchone()
    return {int(k): v for k, v in (json.loads(r["value"]) if r and r["value"] else {}).items() if v}


def camera_zones() -> dict[int, dict]:
    """camera id -> {zone, source}: where each camera looks, from configuration only."""
    from app import visitors

    out: dict[int, dict] = {}
    try:
        visitor_areas = visitors.list_zones()
    except sqlite3.OperationalError:
        visitor_areas = []   # Visitor Management never set up
    for z in visitor_areas:
        if z["enabled"]:
            out[z["camera_id"]] = {"zone": "Visitor seating area", "source": "Visitor Management area"}
    for cid in workforce._gate_cameras():
        out[cid] = {"zone": "Main gate", "source": "Staff Count gate line"}
    for c in get_main_hall()["cameras"]:
        if c:
            out[c["camera_id"]] = {"zone": "Main Hall", "source": "Main Hall cameras setting"}
    for cid, zone in _admin_zones().items():
        out[cid] = {"zone": zone, "source": "set by an admin"}
    return out


def set_camera_zone(camera_id: int, zone: str | None, actor: dict | None = None) -> dict:
    if camera_db.get_camera(int(camera_id)) is None:
        raise ValueError("Camera not found.")
    zone = (zone or "").strip()[:60] or None
    zones = _admin_zones()
    if zone:
        zones[int(camera_id)] = zone
    else:
        zones.pop(int(camera_id), None)
    with _conn() as conn:
        conn.execute("INSERT OR REPLACE INTO presence_settings (key, value, updated_at, updated_by) VALUES ('camera_zones', ?, ?, ?)",
                     (json.dumps({str(k): v for k, v in zones.items()}), time.time(), audit._actor(actor)))
    audit.record("presence.camera_zone", actor, f"camera:{camera_id}", zone=zone)
    return camera_directory()


def _availability(now: float) -> dict[int, str]:
    """camera id -> online (frames arriving) | connecting | offline. A camera
    whose stream is not running yet is 'idle': it connects when opened."""
    from app import camera_stream

    out = {}
    for st in camera_stream.all_status():
        fresh = st["last_frame_at"] is not None and now - st["last_frame_at"] < STREAM_FRESH_SECONDS
        state = st["state"]
        if state == camera_stream.S_RUNNING:
            out[st["camera_id"]] = "online" if fresh else "no video"
        elif state in (camera_stream.S_CONNECTING, camera_stream.S_RECONNECTING, camera_stream.S_STALLED):
            out[st["camera_id"]] = "connecting"
        elif state == camera_stream.S_IDLE:
            out[st["camera_id"]] = "idle"
        else:   # FAILED, STOPPED
            out[st["camera_id"]] = "offline"
    return out


def camera_directory(now: float | None = None) -> list[dict]:
    """Every camera with its mapped location and whether it is streaming now."""
    now = now if now is not None else time.time()
    zones = camera_zones()
    avail = _availability(now)
    out = []
    for cam in camera_db.list_cameras():
        z = zones.get(cam["id"])
        live_on = bool(cam.get("live_feed_enabled", 1))
        out.append({"camera_id": cam["id"], "name": cam["name"], "site": cam.get("site"), "purpose": cam.get("purpose"),
                    "zone": z["zone"] if z else None, "zone_source": z["source"] if z else None,
                    "availability": "feed off" if not live_on else avail.get(cam["id"], "idle")})
    return out


def _recommend(evidence: list[dict], directory: list[dict], gate_cams: set[int], now: float) -> dict:
    by_id = {c["camera_id"]: c for c in directory}
    usable = lambda c: c and c["availability"] != "feed off"   # noqa: E731
    rank = {"online": 0, "connecting": 1, "idle": 2, "no video": 3, "offline": 4, "feed off": 5}

    def pick(ev, why):
        cam = by_id.get(ev["camera_id"])
        if cam is None:
            return None
        zone = cam["zone"]
        same_zone = [c for c in directory if zone and c["zone"] == zone and c["camera_id"] != cam["camera_id"] and usable(c)]
        same_zone.sort(key=lambda c: rank[c["availability"]])
        chosen, note = cam, None
        if cam["availability"] in ("offline", "feed off", "no video") and same_zone and same_zone[0]["availability"] in ("online", "idle", "connecting"):
            chosen, note = same_zone[0], f"{cam['name']} is {cam['availability']}; {same_zone[0]['name']} covers the same location ({zone})."
            same_zone = [cam] + same_zone[1:]
        age = max(0, now - ev["at"]) if ev.get("at") else None
        return {
            "camera": chosen, "evidence_camera_id": ev["camera_id"], "reason": why, "note": note,
            "evidence": {"kind": ev["kind"], "at": ev["at"], "live": ev["live"], "verified": ev["verified"],
                         "confidence": ev.get("confidence"), "age_seconds": age, "detail": ev["detail"]},
            "alternatives": same_zone,
            "gate_camera": ev["camera_id"] in gate_cams,
        }

    with_cam = [e for e in evidence if e.get("camera_id") is not None]
    live_face = next((e for e in with_cam if e["kind"] == "live_face"), None)
    if live_face:
        r = pick(live_face, "Recognised by face on this camera right now.")
        if r:
            return {"state": "live", **r}
    verified = sorted((e for e in with_cam if e["verified"] and e["kind"] in ("face", "footfall")), key=lambda e: -e["at"])
    if verified:
        r = pick(verified[0], "Their most recent face recognition was on this camera. That is historical: they may have moved since.")
        if r:
            return {"state": "historical", **r}
    lead = next((e for e in with_cam if e["kind"] == "live_appearance"), None)
    if lead:
        r = pick(lead, "A person here is matched to them by body appearance only - not a verified identity.")
        if r:
            return {"state": "lead", **r}
    return {"state": "unavailable", "camera": None, "reason": "Camera location unavailable: no camera has identified this employee.",
            "note": None, "evidence": None, "alternatives": [], "gate_camera": False}


# --------------------------------------------------------------------------- evidence

def _live_tracks(employee_id: str) -> list[dict]:
    """This employee in the face pipelines' live tracks right now, per camera."""
    from app import face_pipeline

    out = []
    for cam in camera_db.list_cameras():
        pipe = face_pipeline.get_existing_pipeline(cam["id"])
        if pipe is None:
            continue
        try:
            dets = pipe.get_live_detections()
        except Exception:
            continue
        for d in dets:
            if str(d.get("employee_id") or "") != str(employee_id):
                continue
            out.append({"camera_id": cam["id"], "source": d.get("identity_source"), "confidence": d.get("confidence"),
                        "track_id": d.get("track_id")})
    return out


def _face_events(employee_id: str, t0: float, t1: float) -> list[dict]:
    with _conn() as conn:
        try:
            rows = conn.execute("SELECT camera_id, first_seen, last_seen, sightings, best_confidence FROM face_recognition_events "
                                "WHERE employee_id = ? AND last_seen >= ? AND first_seen < ? ORDER BY last_seen DESC",
                                (employee_id, t0, t1)).fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(r) for r in rows]


def _footfall_entries(employee_id: str, day: str) -> list[dict]:
    from app.reid import reid_db

    try:
        with reid_db.get_connection() as conn:
            rows = conn.execute("SELECT camera_id, ts, employee_confidence FROM footfall_clips WHERE day = ? AND employee_id = ?",
                                (day, employee_id)).fetchall()
    except sqlite3.Error:
        return []
    return [{"camera_id": r[0], "ts": r[1], "confidence": r[2]} for r in rows]


def investigate(employee_id: str, day: str | None = None, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    today = datetime.date.fromtimestamp(now).isoformat()
    day = day or today
    datetime.date.fromisoformat(day)
    emp = employee_directory.get_employee(employee_id)
    if emp is None:
        raise KeyError(employee_id)
    row = workforce.classify_range(day, day, employee_ids=[employee_id], now=now)[0]
    cams = {c["id"]: c for c in camera_db.list_cameras()}
    gate_cams = workforce._gate_cameras()
    cam_name = lambda cid: (cams.get(cid) or {}).get("name") or (f"Camera {cid}" if cid is not None else "Unknown camera")  # noqa: E731
    t0 = workforce._at(day, "00:00")
    t1 = t0 + 86400
    is_today = day == today

    evidence: list[dict] = []
    live = _live_tracks(employee_id) if is_today else []
    for lt in live:
        verified = lt["source"] == "face"
        evidence.append({
            "kind": "live_face" if verified else "live_appearance", "camera_id": lt["camera_id"], "camera": cam_name(lt["camera_id"]),
            "at": now, "live": True, "verified": verified, "confidence": lt["confidence"],
            "detail": ("Recognised by face right now" if verified
                       else "A person here is matched to this employee by body appearance only - not verified by face"),
        })
    for ev in _face_events(employee_id, t0, t1):
        evidence.append({
            "kind": "face", "camera_id": ev["camera_id"], "camera": cam_name(ev["camera_id"]), "at": ev["last_seen"],
            "from": ev["first_seen"], "live": False, "verified": True, "confidence": ev["best_confidence"],
            "sightings": ev["sightings"],
            "detail": "Recognised by face" + (" (gate camera: may be outside)" if ev["camera_id"] in gate_cams else ""),
        })
    for s in row["sessions"]:
        evidence.append({"kind": "gate", "camera_id": None, "camera": "Main gate (Staff Count)", "at": s["exit"] or s["entry"],
                         "from": s["entry"], "live": False, "verified": True,
                         "detail": "Came in through the gate" + (", still inside" if s["inside"] else
                                                                  f", left {'(confirmed)' if s['exit_confirmed'] else '(exit not confirmed)'}")})
    for f in _footfall_entries(employee_id, day):
        evidence.append({"kind": "footfall", "camera_id": f["camera_id"], "camera": cam_name(f["camera_id"]), "at": f["ts"],
                         "live": False, "verified": True, "confidence": f["confidence"], "detail": "Entered through a footfall gate, named by face"})
    evidence.sort(key=lambda e: (not e["live"], -(e["at"] or 0)))

    # Per camera: the newest evidence there.
    per_camera: dict = {}
    for e in evidence:
        if e["camera_id"] is None:
            continue
        cur = per_camera.get(e["camera_id"])
        if cur is None or (e["live"] and not cur["live"]) or (e["live"] == cur["live"] and e["verified"] and not cur["verified"]):
            per_camera[e["camera_id"]] = e
    cameras = [{"camera_id": cid, "camera": e["camera"], "site": (cams.get(cid) or {}).get("site"),
                "purpose": (cams.get(cid) or {}).get("purpose"), "gate": cid in gate_cams,
                "last_seen": e["at"], "live": e["live"], "verified": e["verified"], "kind": e["kind"]}
               for cid, e in per_camera.items()]
    cameras.sort(key=lambda c: (not c["live"], -(c["last_seen"] or 0)))

    verified = [e for e in evidence if e["verified"] and e["kind"] in ("live_face", "face", "footfall")]
    last = max(verified, key=lambda e: e["at"], default=None)
    live_face = next((e for e in evidence if e["kind"] == "live_face"), None)
    appearance = next((e for e in evidence if e["kind"] == "live_appearance"), None)
    recent_inside = next((e for e in verified if e["kind"] == "face" and e["camera_id"] not in gate_cams
                          and now - e["at"] <= SEEN_INSIDE_MINUTES * 60), None) if is_today else None
    clock = lambda t: datetime.datetime.fromtimestamp(t).strftime("%I:%M %p").lstrip("0")  # noqa: E731
    if live_face:
        verdict = {"state": "verified_live", "label": f"Verified now on {live_face['camera']}",
                   "detail": "Recognised by face on this camera right now."}
    elif recent_inside:
        verdict = {"state": "verified_recent", "label": f"Recently seen on {recent_inside['camera']}",
                   "detail": f"Recognised by face at {clock(recent_inside['at'])}; not on a camera at this moment."}
    elif last:
        verdict = {"state": "seen_earlier", "label": "Presence not verified now",
                   "detail": f"Last recognised by face on {last['camera']} at {clock(last['at'])}."}
    else:
        verdict = {"state": "unverified", "label": "Presence not verified",
                   "detail": ("A person on " + appearance["camera"] + " is matched by appearance only; check the cameras."
                              if appearance else "No camera has recognised this employee " + ("today." if is_today else "on this day."))}
    if appearance and verdict["state"] != "verified_live":
        verdict["lead"] = f"Possible match by appearance on {appearance['camera']} now (not verified)"

    directory = camera_directory(now)
    zones = {c["camera_id"]: c["zone"] for c in directory}
    for c in cameras:
        c["zone"] = zones.get(c["camera_id"])
    for e in evidence:
        e["zone"] = zones.get(e["camera_id"]) if e.get("camera_id") is not None else "Main gate"
    return {
        "employee": {"employee_id": employee_id, "name": emp.get("name"), "department": emp.get("company") or None},
        "recommendation": _recommend(evidence, directory, gate_cams, now),
        "camera_directory": directory,
        "day": day, "is_today": is_today, "checked_at": now,
        "attendance": {k: row.get(k) for k in ("status", "first_in", "last_out", "location", "location_detail", "location_at",
                                                "inside_now", "outside_now", "missing_entry", "missing_exit", "late", "half_day",
                                                "corrected", "presence_verified", "original_first_in", "original_last_out", "source")},
        "last_detection": ({"at": last["at"], "camera_id": last["camera_id"], "camera": last["camera"], "live": last["live"],
                            "kind": last["kind"]} if last else None),
        "verdict": verdict,
        "cameras": cameras,
        "evidence": evidence[:60],
        "live_cameras": sorted({e["camera_id"] for e in evidence if e["live"]}),
        "corrections": workforce.list_corrections(employee_id, day),
        "main_hall": get_main_hall(),
    }
