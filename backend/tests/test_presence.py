"""Presence investigation (presence.py) and manual attendance corrections
(workforce.add_correction / void_correction): evidence is only what was
recorded or is tracked live, an appearance-only match is never "verified",
nothing changes attendance by itself, and every correction keeps the original
times and its history."""

import datetime
import time

import pytest

from app import attendance, camera_db, employee_directory, face_pipeline, presence, workforce
from app.reid import reid_db
from app.staff import occupancy
from tests.conftest import bearer

ADMIN = {"principal_type": "admin", "email": "hr@example.com"}


@pytest.fixture
def pr(isolated_db, monkeypatch):
    for mod in (workforce, attendance, occupancy, presence):
        monkeypatch.setattr(mod, "DB_PATH", isolated_db)
    monkeypatch.setattr(employee_directory, "EMPLOYEES", {
        "E1": {"name": "Asha", "company": "Sales"},
        "E2": {"name": "Bala", "company": "Accounts"},
    })
    monkeypatch.setattr(employee_directory, "get_employee", lambda e: employee_directory.EMPLOYEES.get(e))
    monkeypatch.setattr(face_pipeline, "get_existing_pipeline", lambda cid: None)
    attendance.init_db()
    workforce.init_db()
    presence.init_db()
    reid_db.init_db()
    workforce.set_policy({"shift_start": "09:00", "grace_minutes": 30, "shift_end": "18:00", "half_day_after": "12:00",
                          "weekly_offs": [], "overtime_after_minutes": 30, "long_outside_minutes": 60}, ADMIN)
    return isolated_db


def today_at(hh, mm=0):
    return datetime.datetime.combine(datetime.date.today(), datetime.time(hh, mm)).timestamp()


def row(emp="E1", now=None):
    d = datetime.date.today().isoformat()
    return workforce.classify_range(d, d, employee_ids=[emp], now=now or time.time())[0]


def face_event(emp, cam, first, last, conf=0.8):
    with presence._conn() as conn:
        conn.execute("INSERT INTO face_recognition_events (employee_id, camera_id, first_seen, last_seen, sightings, best_confidence) "
                     "VALUES (?, ?, ?, ?, 3, ?)", (emp, cam, first, last, conf))


class FakePipe:
    def __init__(self, dets):
        self.dets = dets

    def get_live_detections(self):
        return self.dets


# ---------------------------------------------------------------- corrections

def test_presence_verified_turns_absent_into_attended_and_keeps_history(pr):
    now = today_at(13)
    assert row(now=now)["status"] == "Absent"
    d = datetime.date.today().isoformat()
    with pytest.raises(ValueError, match="reason"):
        workforce.add_correction("E1", d, "presence_verified", today_at(9, 10), "", actor=ADMIN, now=now)
    r = workforce.add_correction("E1", d, "presence_verified", today_at(9, 10), "Seen at desk on Main Hall camera",
                                 evidence="recognised on CAM2 at 9:10 AM", actor=ADMIN, now=now)["row"]
    assert r["status"] == "Present" and r["first_in"] == today_at(9, 10) and r["presence_verified"] and r["corrected"]
    assert r["original_first_in"] is None                      # the record itself is unchanged
    hist = workforce.list_corrections("E1", d)
    assert [c["kind"] for c in hist] == ["presence_verified"] and hist[0]["created_by"] == "hr@example.com"
    assert hist[0]["evidence"] == "recognised on CAM2 at 9:10 AM"

    workforce.void_correction(hist[0]["id"], "Wrong person", ADMIN, now=now)
    assert row(now=now)["status"] == "Absent"
    hist = workforce.list_corrections("E1", d)
    assert hist[0]["voided"] and hist[0]["void_reason"] == "Wrong person" and hist[0]["voided_by"] == "hr@example.com"
    with pytest.raises(ValueError, match="already withdrawn"):
        workforce.void_correction(hist[0]["id"], "again", ADMIN, now=now)


def test_entry_and_exit_corrections_override_recorded_times(pr):
    now = today_at(19)
    with presence._conn() as conn:
        conn.execute("INSERT INTO attendance_days (employee_id, day, first_seen, last_seen, first_camera, last_camera, sightings, best_confidence) "
                     "VALUES ('E1', ?, ?, ?, 1, 1, 5, 0.9)", (datetime.date.today().isoformat(), today_at(10, 5), today_at(17)))
    before = row(now=now)
    assert before["status"] == "Late" and before["missing_entry"]
    d = datetime.date.today().isoformat()
    r = workforce.add_correction("E1", d, "entry", today_at(9, 0), "Came in with a visitor, gate missed", actor=ADMIN, now=now)["row"]
    assert r["status"] == "Present" and r["first_in"] == today_at(9) and r["original_first_in"] == today_at(10, 5)
    assert not r["missing_entry"]
    r = workforce.add_correction("E1", d, "exit", today_at(18, 45), "Left late", actor=ADMIN, now=now)["row"]
    assert r["last_out"] == today_at(18, 45) and r["overtime_minutes"] == 45
    # A newer correction of the same kind replaces the old one (both kept).
    workforce.add_correction("E1", d, "entry", today_at(9, 20), "Checked CCTV again", actor=ADMIN, now=now)
    hist = [c for c in workforce.list_corrections("E1", d) if c["kind"] == "entry"]
    assert [c["voided"] for c in hist] == [True, False] and row(now=now)["first_in"] == today_at(9, 20)


@pytest.mark.parametrize("kind, ts, msg", [
    ("lunch", 9, "Correction must"),
    ("entry", -1, "same day"),
    ("exit", 8, "before the entry"),
])
def test_correction_validation(pr, kind, ts, msg):
    now = today_at(19)
    d = datetime.date.today().isoformat()
    workforce.add_correction("E1", d, "entry", today_at(9), "gate missed", actor=ADMIN, now=now)
    t = today_at(ts) if ts >= 0 else today_at(0) - 3600
    with pytest.raises(ValueError, match=msg):
        workforce.add_correction("E1", d, kind, t, "a reason", actor=ADMIN, now=now)


def test_future_time_is_refused(pr):
    now = today_at(10)
    with pytest.raises(ValueError, match="future"):
        workforce.add_correction("E1", datetime.date.today().isoformat(), "entry", today_at(11), "x reason", actor=ADMIN, now=now)


# ---------------------------------------------------------------- investigation

def test_no_evidence_means_not_verified(pr):
    r = presence.investigate("E1", now=today_at(12))
    assert r["verdict"]["state"] == "unverified" and r["verdict"]["label"] == "Presence not verified"
    assert r["cameras"] == [] and r["last_detection"] is None
    assert r["employee"] == {"employee_id": "E1", "name": "Asha", "department": "Sales"}


def test_recorded_face_events_recent_and_earlier(pr):
    cam = camera_db.add_camera("CAM2", "HQ")
    now = today_at(12)
    face_event("E1", cam, today_at(9, 5), today_at(11, 50))
    r = presence.investigate("E1", now=now)
    assert r["verdict"]["state"] == "verified_recent" and r["last_detection"]["camera"] == "CAM2"
    assert r["cameras"][0] == {**r["cameras"][0], "camera": "CAM2", "live": False, "verified": True}
    r = presence.investigate("E1", now=today_at(16))
    assert r["verdict"]["state"] == "seen_earlier" and r["verdict"]["label"] == "Presence not verified now"
    assert r["attendance"]["status"] == "Absent"            # face events alone are not attendance here; nothing was changed
    assert workforce.list_corrections("E1") == []


def test_live_face_is_verified_but_appearance_only_is_a_lead(pr, monkeypatch):
    a = camera_db.add_camera("CAM2", "HQ")
    b = camera_db.add_camera("CAM4", "HQ")
    pipes = {a: FakePipe([{"employee_id": "E1", "identity_source": "appearance", "confidence": 0.7, "track_id": 3}]),
             b: FakePipe([{"employee_id": "E2", "identity_source": "face", "confidence": 0.9, "track_id": 1}])}
    monkeypatch.setattr(face_pipeline, "get_existing_pipeline", lambda cid: pipes.get(cid))
    r = presence.investigate("E1", now=today_at(11))
    assert r["verdict"]["state"] == "unverified" and "appearance" in r["verdict"]["detail"]
    ev = r["evidence"][0]
    assert ev["kind"] == "live_appearance" and ev["live"] and not ev["verified"] and ev["camera"] == "CAM2"

    pipes[b] = FakePipe([{"employee_id": "E1", "identity_source": "face", "confidence": 0.88, "track_id": 1}])
    r = presence.investigate("E1", now=today_at(11))
    assert r["verdict"]["state"] == "verified_live" and r["verdict"]["label"] == "Verified now on CAM4"
    assert "lead" not in r["verdict"] and r["live_cameras"] == sorted([a, b])   # verified by face: no appearance lead needed
    assert r["last_detection"]["camera"] == "CAM4" and r["last_detection"]["live"]


def test_past_day_has_no_live_evidence(pr, monkeypatch):
    cam = camera_db.add_camera("CAM2", "HQ")
    monkeypatch.setattr(face_pipeline, "get_existing_pipeline",
                        lambda cid: FakePipe([{"employee_id": "E1", "identity_source": "face", "confidence": 0.9}]))
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    r = presence.investigate("E1", yesterday, now=today_at(12))
    assert r["live_cameras"] == [] and r["verdict"]["state"] == "unverified" and cam


# ---------------------------------------------------------------- Main Hall cameras

def test_main_hall_cameras_are_a_saved_choice(pr):
    assert presence.get_main_hall() == {"cameras": [None, None], "configured": False, "updated_at": None, "updated_by": None}
    a = camera_db.add_camera("CAM2", "HQ")
    b = camera_db.add_camera("CAM4", "HQ")
    with pytest.raises(ValueError, match="different"):
        presence.set_main_hall([a, a], ADMIN)
    with pytest.raises(ValueError, match="not found"):
        presence.set_main_hall([a, 999], ADMIN)
    r = presence.set_main_hall([a, b], ADMIN)
    assert r["configured"] and [c["name"] for c in r["cameras"]] == ["CAM2", "CAM4"] and r["updated_by"] == "hr@example.com"


# ---------------------------------------------------------------- API

def test_api(api, admin_token, pr):
    h = bearer(admin_token)
    assert api.get("/api/attendance/employees/E1/presence").status_code == 401
    r = api.get("/api/attendance/employees/E1/presence", headers=h)
    assert r.status_code == 200 and r.json()["verdict"]["state"] == "unverified"
    assert api.get("/api/attendance/employees/NOPE/presence", headers=h).status_code == 404
    d = datetime.date.today().isoformat()
    ts = datetime.datetime.combine(datetime.date.today(), datetime.time(0, 30)).timestamp()
    bad = api.post("/api/attendance/corrections", json={"employee_id": "E1", "day": d, "kind": "entry", "ts": ts, "reason": ""}, headers=h)
    assert bad.status_code == 422
    ok = api.post("/api/attendance/corrections", json={"employee_id": "E1", "day": d, "kind": "presence_verified", "ts": ts,
                                                        "reason": "seen on CCTV"}, headers=h)
    assert ok.status_code == 200 and ok.json()["row"]["presence_verified"]
    cid = ok.json()["correction_id"]
    hist = api.get(f"/api/attendance/employees/E1/corrections?date={d}", headers=h).json()["corrections"]
    assert hist[0]["id"] == cid
    assert api.post(f"/api/attendance/corrections/{cid}/withdraw", json={"reason": "mistake"}, headers=h).status_code == 200
    a = camera_db.add_camera("CAM2", "HQ")
    b = camera_db.add_camera("CAM4", "HQ")
    assert api.put("/api/attendance/main-hall", json={"camera_ids": [a, b]}, headers=h).json()["configured"]
    assert [c["camera_id"] for c in api.get("/api/attendance/main-hall", headers=h).json()["cameras"]] == [a, b]


# ---------------------------------------------------------------- camera recommendation

def test_recommendation_prefers_live_face_then_latest_recognition(pr, monkeypatch):
    hall1 = camera_db.add_camera("CAM2", "HQ")
    hall2 = camera_db.add_camera("CAM4", "HQ")
    rec_cam = camera_db.add_camera("Reception", "HQ")
    presence.set_main_hall([hall1, hall2], ADMIN)
    presence.set_camera_zone(rec_cam, "Reception", ADMIN)
    monkeypatch.setattr(presence, "_availability", lambda now: {hall1: "online", hall2: "online", rec_cam: "online"})

    r = presence.investigate("E1", now=today_at(12))["recommendation"]
    assert r["state"] == "unavailable" and r["camera"] is None and "Camera location unavailable" in r["reason"]

    face_event("E1", rec_cam, today_at(9), today_at(9, 30))
    face_event("E1", hall1, today_at(10), today_at(11, 40))
    r = presence.investigate("E1", now=today_at(12))["recommendation"]
    assert r["state"] == "historical" and r["camera"]["name"] == "CAM2" and r["camera"]["zone"] == "Main Hall"
    assert [c["name"] for c in r["alternatives"]] == ["CAM4"]               # the other Main Hall camera
    assert not r["evidence"]["live"] and r["evidence"]["age_seconds"] == 20 * 60
    assert "may have moved" in r["reason"]

    face_event("E1", rec_cam, today_at(11, 45), today_at(11, 55))         # newest: reception
    r = presence.investigate("E1", now=today_at(12))["recommendation"]
    assert r["camera"]["name"] == "Reception" and r["alternatives"] == []

    monkeypatch.setattr(face_pipeline, "get_existing_pipeline",
                        lambda cid: FakePipe([{"employee_id": "E1", "identity_source": "face", "confidence": 0.9}]) if cid == hall2 else None)
    r = presence.investigate("E1", now=today_at(12))["recommendation"]
    assert r["state"] == "live" and r["camera"]["name"] == "CAM4" and r["evidence"]["live"] and r["evidence"]["verified"]


def test_offline_camera_is_swapped_for_one_covering_the_same_location(pr, monkeypatch):
    hall1 = camera_db.add_camera("CAM2", "HQ")
    hall2 = camera_db.add_camera("CAM4", "HQ")
    presence.set_main_hall([hall1, hall2], ADMIN)
    monkeypatch.setattr(presence, "_availability", lambda now: {hall1: "offline", hall2: "online"})
    face_event("E1", hall1, today_at(10), today_at(11))
    r = presence.investigate("E1", now=today_at(12))["recommendation"]
    assert r["camera"]["name"] == "CAM4" and r["evidence_camera_id"] == hall1 and "CAM2 is offline" in r["note"]
    assert [c["name"] for c in r["alternatives"]] == ["CAM2"]


def test_appearance_only_is_a_lead_and_unmapped_cameras_have_no_location(pr, monkeypatch):
    cam = camera_db.add_camera("CAM1", "HQ")
    monkeypatch.setattr(presence, "_availability", lambda now: {})
    monkeypatch.setattr(face_pipeline, "get_existing_pipeline",
                        lambda cid: FakePipe([{"employee_id": "E1", "identity_source": "appearance", "confidence": 0.6}]))
    r = presence.investigate("E1", now=today_at(12))["recommendation"]
    assert r["state"] == "lead" and r["camera"]["name"] == "CAM1" and not r["evidence"]["verified"]
    assert r["camera"]["zone"] is None and r["camera"]["availability"] == "idle"   # never guessed from the name
    d = {c["camera_id"]: c for c in presence.camera_directory()}
    assert d[cam]["zone"] is None
    presence.set_camera_zone(cam, "Store room", ADMIN)
    assert {c["camera_id"]: c for c in presence.camera_directory()}[cam]["zone"] == "Store room"
    presence.set_camera_zone(cam, "", ADMIN)
    assert {c["camera_id"]: c for c in presence.camera_directory()}[cam]["zone"] is None
