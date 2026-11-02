"""The unified zone list (zones.py), with intrusion zones (intrusion.py, the
restricted-area engine ported from MAINDV) as its restricted zones, and footfall
daily history (reid_db.footfall_daily)."""

import datetime
import time

import pytest

from app import alerts, analytics_settings, camera_db, desk_db, intrusion, visitors, zones
from app.reid import reid_db
from tests.conftest import bearer

SQUARE = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.9], [0.1, 0.9]]


@pytest.fixture
def zs(isolated_db, monkeypatch):
    for mod in (intrusion, zones, visitors):
        monkeypatch.setattr(mod, "DB_PATH", isolated_db)
    monkeypatch.setattr(intrusion.service, "_zones", {})
    monkeypatch.setattr(analytics_settings, "enabled", lambda f: True)
    intrusion.init_db()
    visitors.init_db()
    alerts.init_db()
    desk_db.init_db()
    reid_db.init_db()
    return isolated_db


def test_unified_list_shows_every_zone_type_with_activity(zs, monkeypatch):
    a = camera_db.add_camera("CAM2", "HQ")
    b = camera_db.add_camera("Lobby", "HQ")
    g = camera_db.add_camera("Gate", "HQ", purpose="Entry/Exit")
    other = camera_db.add_camera("Hall", "HQ")
    reid_db.set_camera_config(other, enabled=True)        # not an Entry/Exit camera: footfall doesn't count it
    desk_db.create_zone(a, SQUARE, "Desk 4")
    visitors.save_zone(b, SQUARE)
    z = intrusion.create_zone(a, "Store", SQUARE, None, None, area="Back office")
    gate = intrusion.create_zone(b, "Reception Entry Gate", SQUARE, "19:00", "08:00", area="Reception", is_entry_gate=True)
    reid_db.set_camera_config(g, enabled=True)
    reid_db.set_entry_line(g, [[0.1, 0.5], [0.9, 0.5]], 1)
    reid_db.log_event(g, 1, reid_db.EVENT_NEW_PERSON, reid_db.create_person(), 0.9)
    with zones._conn() as conn:
        conn.execute("INSERT INTO visitor_report_log (event_id, camera_id, camera_name, visit_key, alert_seq, entered_at, alert_at) "
                     "VALUES (1, ?, 'Lobby', 'k', 1, ?, ?)", (b, time.time(), time.time()))
        conn.execute("INSERT INTO intrusion_events (zone_id, camera_id, ts, people) VALUES (?, ?, ?, 1)", (z["id"], a, time.time()))
    r = zones.list_all()
    by = {x["key"]: x for x in r["zones"]}
    assert r["counts"] == {"employee": 1, "visitor": 1, "restricted": 2, "entry_exit": 1}
    assert by[f"visitor-{b}"]["activity"]["count"] == 1 and "Recognised employees never do" in by[f"visitor-{b}"]["rule"]
    assert by[f"footfall-{g}"]["shape"] == "line" and by[f"footfall-{g}"]["activity"]["count"] == 1
    assert by["desk-1"]["type"] == "employee" and by["desk-1"]["camera"] == "CAM2"
    store = by[f"intrusion-{z['id']}"]
    assert store["source"] == "intrusion" and store["name"] == "Back office · Store" and store["activity"]["count"] == 1
    g_zone = by[f"intrusion-{gate['id']}"]
    assert g_zone["is_entry_gate"] and "entry gate" in g_zone["rule"] and "19:00–08:00" in g_zone["rule"]


def test_unlicensed_intrusion_zones_are_left_out(zs, monkeypatch):
    from app import master_link

    cam = camera_db.add_camera("Lab", "HQ")
    intrusion.create_zone(cam, "Lab", SQUARE, None, None)
    monkeypatch.setattr(master_link, "feature_allowed", lambda f: f != "intrusion_detection")
    assert zones.list_all()["counts"]["restricted"] == 0


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
    r = api.post("/api/intrusion/zones", json={"camera_id": cam, "name": "Lab", "polygon": SQUARE}, headers=h)
    assert r.status_code == 200, r.text
    zid = r.json()["id"]
    assert api.get("/api/zones", headers=h).json()["counts"]["restricted"] == 1
    assert api.patch(f"/api/intrusion/zones/{zid}", json={"severity": "Nope"}, headers=h).status_code == 422
    assert api.delete(f"/api/intrusion/zones/{zid}", headers=h).status_code == 200
    d = datetime.date.today().isoformat()
    assert api.get(f"/api/footfall/history?from={d}&to={d}", headers=h).json()["days"][0]["day"] == d
    assert api.get("/api/footfall/history?from=2026-10-31&to=2025-01-01", headers=h).status_code == 422
