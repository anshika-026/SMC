"""Visitor history filtered by date (GET /api/visitors/visits?start=&end=)."""

import datetime
import itertools

from app import face_db, visitors
from tests.conftest import bearer

_ids = itertools.count(1)


def _visit(key, day, hour):
    t = datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(hour)).timestamp()
    with visitors._conn() as conn:
        conn.execute("INSERT INTO visitor_report_log (event_id, camera_id, camera_name, visit_key, alert_seq, entered_at, alert_at, left_at, last_seen) "
                     "VALUES (?, 4, 'Cam3', ?, 1, ?, ?, ?, ?)", (next(_ids), key, t - 60, t, t + 300, t + 300))


def test_visits_by_date_range(api, admin_token, isolated_db, monkeypatch):
    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    visitors.init_db()
    _visit("a", "2026-10-29", 10)
    _visit("b", "2026-10-31", 23)      # late evening still belongs to the 31st
    _visit("c", "2026-11-01", 0)       # just after midnight belongs to the 1st
    _visit("d", "2026-11-03", 12)
    h = bearer(admin_token)
    get = lambda q="": api.get(f"/api/visitors/visits?limit=2000{q}", headers=h)  # noqa: E731
    assert [v["visit"] for v in get().json()["visits"]] == ["d", "c", "b", "a"]                     # all time
    assert [v["visit"] for v in get("&start=2026-10-31&end=2026-10-31").json()["visits"]] == ["b"]  # one day, both ends included
    assert [v["visit"] for v in get("&start=2026-10-31&end=2026-11-01").json()["visits"]] == ["c", "b"]
    assert [v["visit"] for v in get("&start=2026-11-01").json()["visits"]] == ["d", "c"]            # open-ended
    assert [v["visit"] for v in get("&end=2026-10-30").json()["visits"]] == ["a"]
    assert get("&start=2026-11-03&end=2026-11-01").status_code == 422
    assert get("&start=yesterday").status_code == 422


def _face(cam, ts, *, emp=None, auto=None, tier=None):
    with visitors._conn() as conn:
        conn.execute("INSERT INTO face_training_captures (camera_id, track_id, captured_at, image_path, employee_id, auto_employee_id, auto_tier) "
                     "VALUES (?, 1, ?, 'x.jpg', ?, ?, ?)", (cam, ts, emp, auto, tier))


def test_each_visit_names_the_faces_seen_during_it(api, admin_token, isolated_db, monkeypatch):
    from app import employee_directory

    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    visitors.init_db()
    face_db.init_face_tables()
    monkeypatch.setattr(employee_directory, "get_employee", lambda e: {"E1": {"name": "Ravi Kumar"}, "E2": {"name": "Neha"}}.get(e))
    _visit("a", "2026-11-03", 10)          # 09:59 -> 10:05
    _visit("b", "2026-11-03", 12)
    _visit("c", "2026-11-03", 14)
    t = datetime.datetime(2026, 11, 3, 10, 2).timestamp()
    for _ in range(3):
        _face(4, t, auto="E1", tier="auto")                # recognised: Ravi
    _face(4, t, auto="E2", tier="review")                  # only a possible match
    _face(5, t, auto="E2", tier="auto")                    # another camera: not this visit
    _face(4, t - 3 * 3600, auto="E2", tier="auto")         # hours earlier: not this visit
    tb = datetime.datetime(2026, 11, 3, 12, 1).timestamp()
    _face(4, tb, auto="E2", tier="review")
    _face(4, tb, tier="unknown")
    rows = {v["visit"]: v["faces_seen"] for v in api.get("/api/visitors/visits", headers=bearer(admin_token)).json()["visits"]}
    assert rows["a"]["employees"] == [{"employee_id": "E1", "name": "Ravi Kumar", "faces": 3}]
    assert [p["name"] for p in rows["a"]["possible"]] == ["Neha"]
    assert rows["b"] == {"employees": [], "possible": [{"employee_id": "E2", "name": "Neha", "faces": 1}], "unknown": 1}
    assert rows["c"] == {"employees": [], "possible": [], "unknown": 0}
