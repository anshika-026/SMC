"""Restricted zones (restricted_zones.py), the unified zone list (zones.py)
and footfall daily history (reid_db.footfall_daily)."""

import datetime
import time

import pytest

from app import alerts, analytics_settings, camera_db, desk_db, restricted_zones, visitors, zones
from app.reid import reid_db
from tests.conftest import bearer

ADMIN = {"principal_type": "admin", "email": "admin@example.com"}
SQUARE = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.9], [0.1, 0.9]]


@pytest.fixture
def zs(isolated_db, monkeypatch):
    for mod in (restricted_zones, zones, visitors):
        monkeypatch.setattr(mod, "DB_PATH", isolated_db)
    monkeypatch.setattr(restricted_zones.service, "_sync", lambda: None)
    monkeypatch.setattr(restricted_zones.service, "_zones", {})
    monkeypatch.setattr(restricted_zones.service, "_present", {})
    monkeypatch.setattr(restricted_zones.service, "_last_alert", {})
    monkeypatch.setattr(analytics_settings, "enabled", lambda f: True)
    restricted_zones.init_db()
    visitors.init_db()
    alerts.init_db()
    desk_db.init_db()
    reid_db.init_db()
    return isolated_db


def person(tid, x=200, y=400):   # feet at (x/1000, y/500) of a 1000x500 frame
    return {"track_id": tid, "confidence": 0.9, "bbox": [x - 40, y - 200, x + 40, y]}


def looks(svc, cam, people, t0, n, step=0.5):
    for k in range(n):
        svc.process(cam, 1000, 500, people, t0 + k * step)


def open_alerts():
    with alerts._conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM alerts ORDER BY id")]


def test_zone_validation_and_crud(zs):
    cam = camera_db.add_camera("Server room", "HQ")
    with pytest.raises(ValueError, match="name"):
        restricted_zones.create_zone(cam, " ", SQUARE, actor=ADMIN)
    with pytest.raises(ValueError, match="3 points"):
        restricted_zones.create_zone(cam, "Store", SQUARE[:2], actor=ADMIN)
    with pytest.raises(ValueError, match="inside the picture"):
        restricted_zones.create_zone(cam, "Store", [[0, 0], [2, 0], [0, 1]], actor=ADMIN)
    z = restricted_zones.create_zone(cam, "Store", SQUARE, "everyone", "Critical", 5, actor=ADMIN)
    assert z["rule"] == "everyone" and z["severity"] == "Critical" and z["enabled"]
    z = restricted_zones.update_zone(z["id"], {"rule": "non_employees", "enabled": False}, ADMIN)
    assert z["rule"] == "non_employees" and not z["enabled"] and z["name"] == "Store"
    assert restricted_zones.service._zones == {}           # a disabled zone is not monitored
    assert restricted_zones.delete_zone(z["id"], ADMIN) and restricted_zones.list_zones() == []


def test_unknown_person_raises_one_alert_and_employee_never_does(zs, monkeypatch):
    cam = camera_db.add_camera("Server room", "HQ")
    z = restricted_zones.create_zone(cam, "Server room", SQUARE, "non_employees", "High", 10, actor=ADMIN)
    svc = restricted_zones.service
    # The person on the left is not recognised; the one on the right is employee E7.
    monkeypatch.setattr(svc, "identify", lambda c, b, w, h: None if b[0] < 300 else "E7")
    t0 = time.time()
    looks(svc, cam, [person(1, x=200), person(2, x=400)], t0, 4)            # 1.5 s: within the identify grace
    assert open_alerts() == []
    looks(svc, cam, [person(1, x=200), person(2, x=400)], t0 + 2, 12)        # past the grace
    al = open_alerts()
    assert len(al) == 1 and al[0]["event"] == "Restricted area entered" and al[0]["severity"] == "High"
    assert "not a recognised employee" in al[0]["message"] and al[0]["camera_id"] == cam
    act = restricted_zones.activity(z["id"])
    assert {a["employee_id"] for a in act} == {None, "E7"}
    assert [a["alert_id"] is not None for a in sorted(act, key=lambda a: a["track_id"])] == [True, False]
    # Someone else walks in within the cooldown: logged, no second alert.
    looks(svc, cam, [person(3, x=210)], t0 + 10, 14)
    assert len(open_alerts()) == 1 and len(restricted_zones.activity(z["id"])) == 3
    # Outside the polygon: nothing.
    looks(svc, cam, [person(4, x=800)], t0 + 20, 4)
    assert len(restricted_zones.activity(z["id"])) == 3
    # Leaving closes the entry.
    svc.close_departed(t0 + 60)
    assert all(a["left_at"] for a in restricted_zones.activity(z["id"]))


def test_everyone_rule_alerts_even_for_employees(zs, monkeypatch):
    cam = camera_db.add_camera("Lab", "HQ")
    restricted_zones.create_zone(cam, "Lab", SQUARE, "everyone", "Medium", 10, actor=ADMIN)
    svc = restricted_zones.service
    monkeypatch.setattr(svc, "identify", lambda *a: "E1")
    looks(svc, cam, [person(1)], time.time(), 2)
    al = open_alerts()
    assert len(al) == 1 and al[0]["employee_id"] == "E1"


def test_unified_list_shows_every_zone_type_with_activity(zs, monkeypatch):
    a = camera_db.add_camera("CAM2", "HQ")
    b = camera_db.add_camera("Lobby", "HQ")
    g = camera_db.add_camera("Gate", "HQ", purpose="Entry/Exit")
    other = camera_db.add_camera("Hall", "HQ")
    reid_db.set_camera_config(other, enabled=True)        # not an Entry/Exit camera: footfall doesn't count it
    desk_db.create_zone(a, SQUARE, "Desk 4")
    visitors.save_zone(b, SQUARE)
    restricted_zones.create_zone(a, "Store", SQUARE, actor=ADMIN)
    reid_db.set_camera_config(g, enabled=True)
    reid_db.set_entry_line(g, [[0.1, 0.5], [0.9, 0.5]], 1)
    reid_db.log_event(g, 1, reid_db.EVENT_NEW_PERSON, reid_db.create_person(), 0.9)
    with zones._conn() as conn:
        conn.execute("INSERT INTO visitor_report_log (event_id, camera_id, camera_name, visit_key, alert_seq, entered_at, alert_at) "
                     "VALUES (1, ?, 'Lobby', 'k', 1, ?, ?)", (b, time.time(), time.time()))
    r = zones.list_all()
    by = {z["key"]: z for z in r["zones"]}
    assert r["counts"] == {"employee": 1, "visitor": 1, "restricted": 1, "entry_exit": 1}
    assert by[f"visitor-{b}"]["activity"]["count"] == 1 and "Recognised employees never do" in by[f"visitor-{b}"]["rule"]
    assert by[f"footfall-{g}"]["shape"] == "line" and by[f"footfall-{g}"]["activity"]["count"] == 1
    assert by[f"desk-1"]["type"] == "employee" and by[f"desk-1"]["camera"] == "CAM2"


def test_footfall_daily_includes_archived_counts(zs):
    cam = camera_db.add_camera("Gate", "HQ")
    today = datetime.date.today()
    t = datetime.datetime.combine(today, datetime.time(10)).timestamp()
    p1, p2 = reid_db.create_person(), reid_db.create_person()
    reid_db.log_event(cam, 1, reid_db.EVENT_NEW_PERSON, p1, 0.9, now=t)
    reid_db.log_event(cam, 1, reid_db.EVENT_SIGHTING, p1, 0.9, now=t + 60)      # same person: counted once
    reid_db.log_event(cam, 2, reid_db.EVENT_NEW_PERSON, p2, 0.9, now=t + 120)
    reid_db.log_event(cam, 3, reid_db.EVENT_EXIT, None, None, now=t + 300)
    yesterday = t - 86400
    with reid_db.get_connection() as conn:   # an earlier count, kept by a reset
        conn.execute("INSERT INTO footfall_history (person_ref, camera_id, ts, event_type) VALUES ('123-9', ?, ?, 'new_person')", (cam, yesterday))
    days = reid_db.footfall_daily((today - datetime.timedelta(days=2)).isoformat(), today.isoformat())
    assert [d["entries"] for d in days] == [0, 1, 2] and days[-1]["exits"] == 1


def test_api(api, admin_token, zs):
    h = bearer(admin_token)
    cam = camera_db.add_camera("Lab", "HQ")
    assert api.get("/api/zones").status_code == 401
    r = api.post("/api/zones/restricted", json={"camera_id": cam, "name": "Lab", "polygon": SQUARE}, headers=h)
    assert r.status_code == 201, r.text
    zid = r.json()["id"]
    assert api.get("/api/zones", headers=h).json()["counts"]["restricted"] == 1
    assert api.put(f"/api/zones/restricted/{zid}", json={"severity": "Nope"}, headers=h).status_code == 422
    assert api.get(f"/api/zones/restricted/{zid}/activity", headers=h).json() == {"activity": []}
    assert api.delete(f"/api/zones/restricted/{zid}", headers=h).status_code == 200
    d = datetime.date.today().isoformat()
    assert api.get(f"/api/footfall/history?from={d}&to={d}", headers=h).json()["days"][0]["day"] == d
    assert api.get("/api/footfall/history?from=2026-10-31&to=2025-01-01", headers=h).status_code == 422
