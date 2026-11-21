"""Unique footfall (app/site_footfall.py): the people seen each day, each counted once."""

import datetime
import itertools
import json

from app import attendance, employee_directory, face_db, site_footfall, visitors
from tests.conftest import bearer

_ids = itertools.count(1)


def at(d, h, m=0):
    return datetime.datetime(2026, 11, d, h, m).timestamp()


def _seen(emp, cam, first, last):
    with attendance._conn() as conn:
        conn.execute("INSERT INTO face_recognition_events (employee_id, camera_id, first_seen, last_seen, sightings, best_confidence)"
                     " VALUES (?, ?, ?, ?, 1, 0.99)", (emp, cam, first, last))
        day = datetime.datetime.fromtimestamp(first).strftime("%Y-%m-%d")
        conn.execute("INSERT INTO attendance_days (employee_id, day, first_seen, last_seen, sightings) VALUES (?, ?, ?, ?, 10)"
                     " ON CONFLICT(employee_id, day) DO UPDATE SET sightings = sightings + 10", (emp, day, first, last))


def _visitor(key, start, end, face):
    with visitors._conn() as conn:
        conn.execute("INSERT INTO visitor_report_log (event_id, camera_id, camera_name, visit_key, alert_seq, entered_at, alert_at, left_at, last_seen) "
                     "VALUES (?, 4, 'Cam3', ?, 1, ?, ?, ?, ?)", (next(_ids), key, start, start + 30, end, end))
        v = [0.0] * 512
        v[face] = 1.0
        conn.execute("INSERT INTO face_training_captures (camera_id, track_id, captured_at, image_path, auto_tier, embedding) "
                     "VALUES (4, 1, ?, 'x.jpg', 'unknown', ?)", (start + 60, json.dumps(v)))


def test_each_person_once_a_day_with_arrivals_and_peak_hour(api, admin_token, isolated_db, monkeypatch):
    monkeypatch.setattr(attendance, "DB_PATH", isolated_db)
    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    attendance.init_db()
    visitors.init_db()
    face_db.init_face_tables()
    monkeypatch.setattr(employee_directory, "get_employee", lambda e: {"E1": {"name": "Ravi"}, "E2": {"name": "Neha"}}.get(e))
    _seen("E1", 1, at(17, 10, 5), at(17, 10, 30))      # Ravi on two cameras, twice: one person
    _seen("E1", 2, at(17, 10, 20), at(17, 18, 0))
    _seen("E2", 1, at(17, 10, 40), at(17, 17, 0))
    _seen("E1", 1, at(16, 9, 50), at(16, 18, 0))       # the day before
    _visitor("v1", at(17, 11, 40), at(17, 11, 44), face=7)   # one visitor, two visits: once
    _visitor("v2", at(17, 15, 0), at(17, 15, 20), face=7)

    s = site_footfall.day_stats("2026-11-17")
    assert (s["entries"], s["employee_entries"], s["visitor_entries"], s["previous_day_entries"]) == (3, 2, 1, 1)
    assert s["hourly"] == [{"hour": 10, "entries": 2}, {"hour": 11, "entries": 1}] and s["peak_hour"] == 10
    assert s["exits"] == 3                                   # an earlier day: everyone has left
    ppl = site_footfall.people("2026-11-17")
    assert [(p["name"], p["cameras"]) for p in ppl] == [("Ravi", [1, 2]), ("Neha", [1]), ("Visitor #1", [4])]
    assert ppl[0]["first_seen"] == at(17, 10, 5) and ppl[0]["last_seen"] == at(17, 18, 0)

    h = bearer(admin_token)
    days = api.get("/api/footfall/history?from=2026-11-15&to=2026-11-17", headers=h).json()["days"]
    assert [(d["day"], d["entries"], d["employee_entries"]) for d in days] == [("2026-11-15", 0, 0), ("2026-11-16", 1, 1), ("2026-11-17", 3, 2)]
    assert api.get("/api/footfall/day?date=2026-11-17", headers=h).json()["entries"] == 3
    names = [p["name"] for p in api.get("/api/footfall/site-people?date=2026-11-17", headers=h).json()["people"]]
    assert names == ["Ravi", "Neha", "Visitor #1"]
