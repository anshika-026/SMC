"""Ported from MAINDV (intrusion-loitering, tests/test_intrusion_zones.py), unchanged except where noted.

Several named zones per camera, restricted vs loitering zones, per-zone alert settings, and the
chain from a delivered person to an alert that names its zone (app/intrusion.py + its routes)."""

import sqlite3
import time

import numpy as np
import pytest

from app import alerts, analytics_settings, camera_db, evidence, intrusion, loitering
from tests.conftest import bearer

DOOR = [[0.05, 0.05], [0.45, 0.05], [0.45, 0.55], [0.05, 0.55]]        # left-top block
WAITING = [[0.5, 0.2], [0.95, 0.2], [0.95, 0.9], [0.5, 0.9]]            # right block
FRAME = np.zeros((500, 1000, 3), dtype=np.uint8)
IN_DOOR = [150, 100, 250, 250]        # feet (200, 250)  -> x 0.20, y 0.50: inside DOOR
IN_WAITING = [650, 100, 750, 420]     # feet (700, 420)  -> x 0.70, y 0.84: inside WAITING
OUTSIDE = [450, 100, 520, 480]        # feet (485, 480)  -> in neither


class FakeEvidence:
    """Stands in for the clip service: records which alerts asked for a clip."""

    def __init__(self):
        self.scheduled = []

    def schedule(self, alert_id, camera_id, event_ts):
        self.scheduled.append((alert_id, camera_id, event_ts))


class Clock:
    def __init__(self):
        self.t = 5000.0

    def __call__(self):
        return self.t


@pytest.fixture
def setup(api, admin_token, monkeypatch):
    intrusion.init_db()
    alerts.init_db()
    monkeypatch.setattr(alerts, "SNAPSHOT_DIR", alerts.SNAPSHOT_DIR.parent / "snaps-test")
    monkeypatch.setattr(analytics_settings, "_state", {k: True for k in analytics_settings.FEATURES})
    svc = intrusion.IntrusionService()
    monkeypatch.setattr(intrusion, "service", svc)
    fake = FakeEvidence()
    monkeypatch.setattr(evidence, "service", fake)
    clock = Clock()
    svc._loiter = loitering.LoiterTracker(clock=clock)
    cam = camera_db.add_camera("Lobby", "Noida Site", host="127.0.0.1", port=554, user="u", password="secret-pw", stream_path="/s")
    return api, bearer(admin_token), cam, svc, fake, clock


def make_zone(api, h, cam, name, polygon, **extra):
    r = api.post("/api/intrusion/zones", json={"camera_id": cam, "name": name, "polygon": polygon, **extra}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def person(bbox, track_id, conf=0.9):
    return {"bbox": bbox, "confidence": conf, "track_id": track_id}


def step(svc, cam, clock, seconds, people, zones=None):
    """One detector cycle `seconds` later, delivered straight to the detection step."""
    clock.t += seconds
    zones = zones if zones is not None else [z for z in intrusion.list_zones(cam) if z["enabled"]]
    boxes = [p["bbox"] for p in people if p["confidence"] >= intrusion.PERSON_CONF]
    svc._detect(cam, FRAME, zones, time.time(), boxes, people)


# ---- several named zones on one camera -------------------------------------------------------

def test_one_camera_can_have_several_named_zones_of_different_types(setup):
    api, h, cam, svc, _, _ = setup
    make_zone(api, h, cam, "Main Door", DOOR)
    make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=900, severity="Medium")
    zones = api.get(f"/api/intrusion/zones?camera_id={cam}", headers=h).json()
    by_name = {z["name"]: z for z in zones}
    assert set(by_name) == {"Main Door", "Waiting Area"}
    assert len({z["id"] for z in zones}) == 2                                   # unique ids
    assert by_name["Main Door"]["zone_type"] == "restricted" and by_name["Main Door"]["dwell_seconds"] is None
    assert (by_name["Waiting Area"]["zone_type"], by_name["Waiting Area"]["dwell_seconds"]) == ("loitering", 900)
    assert by_name["Waiting Area"]["polygon"] == WAITING and by_name["Main Door"]["polygon"] == DOOR
    assert all(z["camera_id"] == cam and z["enabled"] and z["alerts_enabled"] for z in zones)


def test_each_zone_is_edited_renamed_disabled_and_deleted_on_its_own(setup):
    api, h, cam, svc, _, _ = setup
    door = make_zone(api, h, cam, "Main Door", DOOR, severity="Critical")
    wait = make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=900)

    assert api.patch(f"/api/intrusion/zones/{door['id']}", json={"name": "Front Entrance"}, headers=h).status_code == 200
    now = {z["id"]: z for z in intrusion.list_zones(cam)}
    assert now[door["id"]]["name"] == "Front Entrance" and now[door["id"]]["severity"] == "Critical"
    assert now[wait["id"]]["name"] == "Waiting Area" and now[wait["id"]]["dwell_seconds"] == 900   # untouched

    api.patch(f"/api/intrusion/zones/{wait['id']}", json={"dwell_seconds": 1200, "alerts_enabled": False}, headers=h)
    now = {z["id"]: z for z in intrusion.list_zones(cam)}
    assert (now[wait["id"]]["dwell_seconds"], now[wait["id"]]["alerts_enabled"]) == (1200, False)
    assert now[door["id"]]["alerts_enabled"] is True

    api.patch(f"/api/intrusion/zones/{door['id']}", json={"enabled": False}, headers=h)
    assert [z["name"] for z in svc._zones[cam]] == ["Waiting Area"]            # the detector only sees the other one

    assert api.delete(f"/api/intrusion/zones/{wait['id']}", headers=h).status_code == 200
    left = intrusion.list_zones(cam)
    assert [z["id"] for z in left] == [door["id"]] and left[0]["enabled"] is False


def test_zones_and_their_settings_survive_a_restart(setup):
    api, h, cam, _, _, _ = setup
    make_zone(api, h, cam, "Main Door", DOOR, active_from="19:00", active_to="08:00", severity="High")
    make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=900, grace_seconds=20, alerts_enabled=False)
    restarted = intrusion.IntrusionService()                                   # a new process: nothing in memory
    restarted.refresh()
    zones = {z["name"]: z for z in restarted._zones[cam]}
    assert zones["Main Door"]["active_from"] == "19:00" and zones["Main Door"]["zone_type"] == "restricted"
    w = zones["Waiting Area"]
    assert (w["zone_type"], w["dwell_seconds"], w["grace_seconds"], w["alerts_enabled"], w["polygon"]) == ("loitering", 900, 20.0, False, WAITING)


def test_zones_saved_before_this_feature_become_restricted_zones(tmp_path, monkeypatch):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("""CREATE TABLE intrusion_zones (id INTEGER PRIMARY KEY AUTOINCREMENT, camera_id INTEGER NOT NULL,
                        name TEXT NOT NULL, polygon TEXT NOT NULL, active_from TEXT, active_to TEXT,
                        enabled INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL)""")
        conn.execute("INSERT INTO intrusion_zones (camera_id, name, polygon, enabled, created_at) VALUES (3, 'Old zone', '[[0,0],[1,0],[1,1]]', 1, 1)")
    monkeypatch.setattr(intrusion, "DB_PATH", old)
    intrusion.init_db()
    intrusion.init_db()                                                         # running it twice is harmless
    (zone,) = intrusion.list_zones()
    assert (zone["name"], zone["zone_type"], zone["alerts_enabled"], zone["dwell_seconds"]) == ("Old zone", "restricted", True, None)


# ---- the existing door / entry rule is unchanged ---------------------------------------------

def test_restricted_zone_still_alerts_when_feet_are_inside_and_names_its_zone(setup):
    api, h, cam, svc, fake, clock = setup
    door = make_zone(api, h, cam, "Main Door", DOOR)
    step(svc, cam, clock, 1, [person(IN_DOOR, 1), person(OUTSIDE, 2)])
    step(svc, cam, clock, 5, [person(IN_DOOR, 1)])                              # still there: same alert, counted again
    (row,) = alerts.list_alerts("all")
    assert (row["event"], row["severity"], row["occurrences"]) == ("Intrusion Detected", "High", 2)
    assert (row["zone_id"], row["zone_name"]) == (door["id"], "Main Door")
    assert "1 person inside Main Door" in row["message"] and row["has_snapshot"]
    assert [(a, c) for a, c, _ in fake.scheduled] == [(row["id"], cam)]         # one clip, for the one alert


def test_a_person_outside_every_zone_raises_nothing(setup):
    api, h, cam, svc, _, clock = setup
    make_zone(api, h, cam, "Main Door", DOOR)
    make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=5)
    for _ in range(10):
        step(svc, cam, clock, 2, [person(OUTSIDE, 4)])
    assert alerts.list_alerts("all") == []


def test_zone_with_alerts_switched_off_logs_the_event_but_raises_no_alert(setup):
    api, h, cam, svc, fake, clock = setup
    make_zone(api, h, cam, "Quiet door", DOOR, alerts_enabled=False)
    step(svc, cam, clock, 1, [person(IN_DOOR, 1)])
    assert alerts.list_alerts("all") == [] and fake.scheduled == []
    assert intrusion.stats()["today"] == 1


def test_per_zone_severity_is_used(setup):
    api, h, cam, svc, _, clock = setup
    make_zone(api, h, cam, "Vault", DOOR, severity="Critical")
    step(svc, cam, clock, 1, [person(IN_DOOR, 1)])
    assert alerts.list_alerts("all")[0]["severity"] == "Critical"


# ---- loitering through the real detection step -----------------------------------------------

def test_person_who_stays_fifteen_minutes_in_the_waiting_area_raises_one_loitering_alert(setup):
    api, h, cam, svc, fake, clock = setup
    wait = make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=900)
    for _ in range(int(900 / 5) - 2):                                            # 14 min 50 s so far
        step(svc, cam, clock, 5, [person(IN_WAITING, 11)])
    assert alerts.list_alerts("all") == []
    for _ in range(6):
        step(svc, cam, clock, 5, [person(IN_WAITING, 11)])
    for _ in range(100):                                                         # keeps standing there
        step(svc, cam, clock, 5, [person(IN_WAITING, 11)])
    (row,) = alerts.list_alerts("all")
    assert row["event"] == "Loitering Detected" and row["severity"] == "Medium"
    assert (row["zone_id"], row["zone_name"]) == (wait["id"], "Waiting Area")
    assert "Waiting Area" in row["message"] and "15 minutes" in row["message"] and row["has_snapshot"]
    assert len(fake.scheduled) == 1 and fake.scheduled[0][0] == row["id"]


def test_loitering_timer_resets_when_the_person_leaves_and_alerts_again_on_a_later_visit(setup):
    api, h, cam, svc, _, clock = setup
    make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=60, grace_seconds=10)
    for _ in range(14):
        step(svc, cam, clock, 5, [person(IN_WAITING, 11)])                      # 70 s inside: first alert
    assert len(alerts.list_alerts("all")) == 1
    for _ in range(5):
        step(svc, cam, clock, 5, [])                                             # leaves for 25 s (> grace)
    for _ in range(8):
        step(svc, cam, clock, 5, [person(IN_WAITING, 11)])                      # 40 s: not yet
    assert len(alerts.list_alerts("all")) == 1
    for _ in range(6):
        step(svc, cam, clock, 5, [person(IN_WAITING, 11)])                      # now over a minute again
    assert len(alerts.list_alerts("all")) == 2


def test_two_people_in_one_zone_get_their_own_timers(setup):
    api, h, cam, svc, _, clock = setup
    make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=60)
    other = [660, 100, 760, 400]
    for _ in range(8):
        step(svc, cam, clock, 5, [person(IN_WAITING, 11)])                      # person 11 alone for 40 s
    assert alerts.list_alerts("all") == []
    for _ in range(5):
        step(svc, cam, clock, 5, [person(IN_WAITING, 11), person(other, 12)])   # person 12 joins
    assert len(alerts.list_alerts("all")) == 1                                   # only 11 has passed a minute
    for _ in range(8):
        step(svc, cam, clock, 5, [person(IN_WAITING, 11), person(other, 12)])
    assert len(alerts.list_alerts("all")) == 2                                   # 12 has now too


def test_door_and_waiting_alerts_on_one_camera_are_told_apart_by_zone(setup):
    api, h, cam, svc, _, clock = setup
    door = make_zone(api, h, cam, "Main Door", DOOR)
    wait = make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=30)
    for _ in range(10):
        step(svc, cam, clock, 5, [person(IN_DOOR, 1), person(IN_WAITING, 2)])
    rows = alerts.list_alerts("all")
    assert {(r["event"], r["zone_name"], r["zone_id"], r["camera_id"]) for r in rows} == {
        ("Intrusion Detected", "Main Door", door["id"], cam),
        ("Loitering Detected", "Waiting Area", wait["id"], cam),
    }
    assert all(r["camera_name"] == "Lobby" for r in rows)


def test_detections_without_a_track_id_cannot_loiter(setup):
    api, h, cam, svc, _, clock = setup
    make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=10)
    for _ in range(10):
        step(svc, cam, clock, 5, [person(IN_WAITING, None)])
    assert alerts.list_alerts("all") == []


def test_loitering_does_not_run_outside_the_zones_active_hours(setup):
    api, h, cam, svc, _, clock = setup
    make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=10)
    for _ in range(10):
        step(svc, cam, clock, 5, [person(IN_WAITING, 5)], zones=[])             # the zone is not active this cycle
    assert alerts.list_alerts("all") == []


def test_loitering_alert_is_off_when_the_zone_alerts_are_off(setup):
    api, h, cam, svc, fake, clock = setup
    make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=10, alerts_enabled=False)
    for _ in range(10):
        step(svc, cam, clock, 5, [person(IN_WAITING, 5)])
    assert alerts.list_alerts("all") == [] and fake.scheduled == []


def test_people_delivered_by_the_shared_detector_reach_the_loitering_check(setup):
    api, h, cam, svc, _, clock = setup
    make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=10)
    for _ in range(4):
        clock.t += 5
        svc.deliver(cam, FRAME, [person(IN_WAITING, 9)], time.time())
        svc._futures[cam].result(timeout=10)
    (row,) = alerts.list_alerts("all")
    assert row["event"] == "Loitering Detected"


# ---- zone API rules --------------------------------------------------------------------------

@pytest.mark.parametrize("extra, why", [
    ({"zone_type": "loitering"}, "no duration"),
    ({"zone_type": "loitering", "dwell_seconds": 2}, "duration too short"),
    ({"zone_type": "loitering", "dwell_seconds": 90000}, "duration over 24 hours"),
    ({"zone_type": "loitering", "dwell_seconds": 60, "grace_seconds": 999}, "grace too long"),
    ({"zone_type": "wandering", "dwell_seconds": 60}, "unknown zone type"),
    ({"severity": "Apocalyptic"}, "unknown severity"),
])
def test_bad_zone_settings_are_refused_with_a_reason(setup, extra, why):
    api, h, cam, *_ = setup
    r = api.post("/api/intrusion/zones", json={"camera_id": cam, "name": "Z", "polygon": WAITING, **extra}, headers=h)
    assert r.status_code == 422, why
    assert isinstance(r.json()["detail"], str) and r.json()["detail"]
    assert intrusion.list_zones() == []


def test_a_zones_outline_can_be_changed_and_a_bad_one_is_refused(setup):
    api, h, cam, *_ = setup
    z = make_zone(api, h, cam, "Main Door", DOOR)
    moved = [[0.1, 0.1], [0.3, 0.1], [0.3, 0.4], [0.1, 0.4]]
    assert api.patch(f"/api/intrusion/zones/{z['id']}", json={"polygon": moved}, headers=h).status_code == 200
    assert intrusion.get_zone(z["id"])["polygon"] == moved
    assert api.patch(f"/api/intrusion/zones/{z['id']}", json={"polygon": [[0.1, 0.1], [0.2, 0.2]]}, headers=h).status_code == 422
    assert intrusion.get_zone(z["id"])["polygon"] == moved


def test_switching_a_loitering_zone_to_restricted_drops_its_duration_and_blank_names_are_refused(setup):
    api, h, cam, *_ = setup
    z = make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=900)
    r = api.patch(f"/api/intrusion/zones/{z['id']}", json={"zone_type": "restricted"}, headers=h)
    assert r.status_code == 200 and r.json()["dwell_seconds"] is None
    assert api.patch(f"/api/intrusion/zones/{z['id']}", json={"name": "   "}, headers=h).status_code == 422
    assert intrusion.get_zone(z["id"])["name"] == "Waiting Area"


def test_changing_a_loitering_zone_to_a_longer_duration_restarts_its_timers(setup):
    api, h, cam, svc, _, clock = setup
    z = make_zone(api, h, cam, "Waiting Area", WAITING, zone_type="loitering", dwell_seconds=60)
    for _ in range(10):
        step(svc, cam, clock, 5, [person(IN_WAITING, 3)])
    assert svc._loiter.seconds_inside(cam, z["id"], 3) is not None
    api.patch(f"/api/intrusion/zones/{z['id']}", json={"dwell_seconds": 600}, headers=h)
    assert svc._loiter.seconds_inside(cam, z["id"], 3) is None


def test_format_duration_reads_naturally():
    assert [intrusion.format_duration(s) for s in (30, 60, 90, 900, 3600, 7200, 1)] == [
        "30 seconds", "1 minute", "1 min 30 s", "15 minutes", "1 hour", "2 hours", "1 second"]


def test_each_intrusion_gets_its_own_alert():
    """People keep being seen: one alert. The zone was empty for longer than
    NEW_ALERT_AFTER_SECONDS: the next person is a new intrusion with its own alert."""
    from app import intrusion

    svc = intrusion.IntrusionService()
    gap = intrusion.NEW_ALERT_AFTER_SECONDS
    k1 = svc.episode_key(15, 1000.0)
    assert svc.episode_key(15, 1000.0 + 30) == k1            # still the same intrusion
    assert svc.episode_key(15, 1030.0 + gap - 1) == k1
    k2 = svc.episode_key(15, 1030.0 + gap - 1 + gap + 5)     # empty for longer than the gap
    assert k2 != k1 and k2.startswith("intrusion:15:")
    assert svc.episode_key(16, 1000.0) != k1                 # another zone


def test_the_alert_names_the_person_inside_the_zone_not_others_in_view(setup, monkeypatch):
    """Face recognition is asked who is at each box INSIDE the zone; someone else the camera
    recognises elsewhere in the picture (e.g. seated nearby) is never named."""
    from app import employee_directory

    api, h, cam, svc, _, _ = setup
    monkeypatch.setattr(employee_directory, "get_employee", lambda e: {"RAHUL": {"name": "Rahul"}, "PUSH": {"name": "Pushpendra"}}.get(e))
    now = time.time()
    a1 = alerts.raise_alert("Intrusion Detected", "High", cam, "1 person inside Door", dedupe_key="intrusion:z:1", now=now - 60)
    a2 = alerts.raise_alert("Intrusion Detected", "High", cam, "1 person inside Door", dedupe_key="intrusion:z:2", now=now - 30)
    gate_box, seat_box = [100, 100, 200, 400], [600, 100, 700, 400]
    known = {tuple(gate_box): "RAHUL", tuple(seat_box): "PUSH"}
    ident = lambda cam_id, b, w, hh: known.get(tuple(b))  # noqa: E731
    svc.name_people(a1, cam, [gate_box], 1280, 720, now, identify=ident)   # only the box inside the zone
    svc.name_people(a2, cam, [[300, 100, 400, 400]], 1280, 720, now, identify=ident)   # face not recognised
    who = {x["id"]: x["who"] for x in api.get("/api/intrusion/alerts", headers=h).json()["alerts"]}
    assert who[a1] == "Rahul" and who[a2] == "Person"
    # A second person coming in while it is still open is added to the same alert.
    svc.name_people(a1, cam, [gate_box, [800, 100, 900, 400]], 1280, 720, now + 2,
                    identify=lambda c, b, w, hh: {tuple(gate_box): "RAHUL"}.get(tuple(b), "PUSH"))
    who = {x["id"]: x["who"] for x in api.get("/api/intrusion/alerts", headers=h).json()["alerts"]}
    assert who[a1] in ("Pushpendra, Rahul", "Rahul, Pushpendra")


def test_alert_on_entry_zone_alerts_when_anyone_comes_in_and_every_15_minutes():
    """A hall: anyone coming in is an alert; staying gives no new alert until 15 min later,
    then one more every 15 min; empty for longer than NEW_ALERT_AFTER_SECONDS = a new stay."""
    from app import intrusion

    svc = intrusion.IntrusionService()
    every = 900
    a = svc.stay_alert(16, 1000.0, every)
    assert a["repeat"] == 0
    keys = {svc.stay_alert(16, 1000.0 + t, every)["key"] for t in range(10, 890, 10)}
    assert keys == {a["key"]}                                  # still there: the same alert
    b = svc.stay_alert(16, 1000.0 + 900, every)
    assert b["repeat"] == 1 and b["key"] != a["key"]           # 15 minutes: one more alert
    assert svc.stay_alert(16, 1000.0 + 1000, every)["key"] == b["key"]
    c = svc.stay_alert(16, 1000.0 + 1000 + intrusion.NEW_ALERT_AFTER_SECONDS + 5, every)
    assert c["repeat"] == 0 and c["key"] not in (a["key"], b["key"])   # left and someone came in again
