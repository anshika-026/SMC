"""Visitor history filtered by date (GET /api/visitors/visits?start=&end=)."""

import datetime
import itertools
import json

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


def _face(cam, ts, *, emp=None, auto=None, tier=None, embedding=None):
    """A face capture; tier "unknown" ones were compared and matched nobody, so they carry an
    embedding (a face direction, see _emb) unless one is given."""
    if embedding is None and tier == "unknown":
        embedding = _emb(0)
    with visitors._conn() as conn:
        conn.execute("INSERT INTO face_training_captures (camera_id, track_id, captured_at, image_path, employee_id, auto_employee_id, auto_tier, embedding) "
                     "VALUES (?, 1, ?, 'x.jpg', ?, ?, ?, ?)", (cam, ts, emp, auto, tier, json.dumps(embedding) if embedding else None))


def _emb(person, noise=0.0):
    """A 512-d unit face embedding for person n (different people are orthogonal)."""
    v = [0.0] * 512
    v[person] = 1.0
    v[person + 100] = noise
    return v


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
    rows = {v["visit"]: v["faces_seen"] for v in api.get("/api/visitors/visits?employee_sittings=true", headers=bearer(admin_token)).json()["visits"]}
    assert rows["a"]["employees"] == [{"employee_id": "E1", "name": "Ravi Kumar", "faces": 3}]
    assert [p["name"] for p in rows["a"]["possible"]] == ["Neha"]
    assert rows["b"] == {"employees": [], "possible": [{"employee_id": "E2", "name": "Neha", "faces": 1}], "unknown": 1}
    assert rows["c"] == {"employees": [], "possible": [], "unknown": 0}


def _employee_stay(emp, day, hour, minutes=6, cam=4):
    t = datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(hour)).timestamp()
    with visitors._conn() as conn:
        conn.execute("INSERT INTO visitor_area_employee_log (camera_id, camera_name, employee_id, entered_at, last_seen, left_at) "
                     "VALUES (?, 'Cam3', ?, ?, ?, ?)", (cam, emp, t - 60, t - 60 + minutes * 60, t - 60 + minutes * 60))


def test_an_employees_sitting_is_not_a_visitor(api, admin_token, isolated_db, monkeypatch):
    """A visitor is someone who is not an employee: a sitting where an employee was recognised, or
    sat there at the time, and no face matched nobody, is the employee's, not a visitor's."""
    from app import employee_directory

    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    visitors.init_db()
    face_db.init_face_tables()
    monkeypatch.setattr(employee_directory, "get_employee", lambda e: {"E1": {"name": "Ravi Kumar"}, "E2": {"name": "Deepak Jha"}}.get(e))
    _visit("recognised", "2026-11-03", 10)     # Ravi's face recognised during it, nothing unknown
    _face(4, datetime.datetime(2026, 11, 3, 10, 2).timestamp(), auto="E1", tier="auto")
    _visit("sat_there", "2026-11-03", 12)      # no face, but Deepak sat in the area at the time
    _employee_stay("E2", "2026-11-03", 12)
    _visit("with_unknown", "2026-11-03", 14)   # Ravi there, and a face that matched nobody
    _face(4, datetime.datetime(2026, 11, 3, 14, 1).timestamp(), auto="E1", tier="auto")
    _face(4, datetime.datetime(2026, 11, 3, 14, 2).timestamp(), tier="unknown")
    _visit("nobody", "2026-11-03", 16)         # no face, no employee there
    _employee_stay("E2", "2026-11-03", 16, cam=5)   # another camera does not count
    h = bearer(admin_token)
    shown = {v["visit"]: v for v in api.get("/api/visitors/visits", headers=h).json()["visits"]}
    assert set(shown) == {"with_unknown", "nobody"}
    assert [e["name"] for e in shown["with_unknown"]["employees_present"]] == ["Ravi Kumar"]
    assert shown["nobody"]["employees_present"] == []
    every = {v["visit"]: v["kind"] for v in api.get("/api/visitors/visits?employee_sittings=true", headers=h).json()["visits"]}
    assert every == {"recognised": "employee", "sat_there": "employee", "with_unknown": "visitor", "nobody": "visitor"}
    k = visitors.kpis(now=datetime.datetime(2026, 11, 3, 20).timestamp(), day="2026-11-03")
    assert k["visitors_today"] == 2


def test_unblurred_faces_only_make_a_visitor(api, admin_token, isolated_db, monkeypatch):
    """A crop too small or blurred to compare (no embedding) is not a face that matched nobody."""
    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    visitors.init_db()
    face_db.init_face_tables()
    _visit("blurry", "2026-11-03", 10)
    _employee_stay("E1", "2026-11-03", 10)
    with visitors._conn() as conn:
        conn.execute("INSERT INTO face_training_captures (camera_id, track_id, captured_at, image_path) VALUES (4, 1, ?, 'x.jpg')",
                     (datetime.datetime(2026, 11, 3, 10, 1).timestamp(),))
    assert api.get("/api/visitors/visits", headers=bearer(admin_token)).json()["visits"] == []


def test_each_visitor_gets_a_number_that_stays_when_they_come_back(api, admin_token, isolated_db, monkeypatch):
    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    visitors.init_db()
    face_db.init_face_tables()
    at = lambda d, h, m: datetime.datetime(2026, 11, d, h, m).timestamp()  # noqa: E731
    _visit("a1", "2026-11-03", 10)                      # person A
    _face(4, at(3, 10, 1), tier="unknown", embedding=_emb(1))
    _visit("b1", "2026-11-03", 12)                      # person B
    _face(4, at(3, 12, 1), tier="unknown", embedding=_emb(2))
    _visit("a2", "2026-11-03", 15)                      # A again, same day
    _face(4, at(3, 15, 1), tier="unknown", embedding=_emb(1, noise=0.3))
    _visit("noface", "2026-11-04", 9)                   # nobody recognised, no face: a number of its own
    _visit("a3", "2026-11-05", 11)                      # A again, another day
    _face(4, at(5, 11, 1), tier="unknown", embedding=_emb(1, noise=-0.2))
    h = bearer(admin_token)
    rows = {v["visit"]: v for v in api.get("/api/visitors/visits", headers=h).json()["visits"]}
    n = {k: v["visitor_number"] for k, v in rows.items()}
    assert n == {"a1": 1, "b1": 2, "a2": 1, "noface": 3, "a3": 1}   # oldest first, A keeps #1
    assert rows["a3"]["visitor_visits"] == 3 and rows["b1"]["visitor_visits"] == 1
    # Numbers never change once given: a later visit by B keeps #2, a new person gets #4.
    _visit("b2", "2026-11-06", 10)
    _face(4, at(6, 10, 1), tier="unknown", embedding=_emb(2))
    _visit("c1", "2026-11-06", 14)
    _face(4, at(6, 14, 1), tier="unknown", embedding=_emb(3))
    n = {v["visit"]: v["visitor_number"] for v in api.get("/api/visitors/visits", headers=h).json()["visits"]}
    assert n == {"a1": 1, "b1": 2, "a2": 1, "noface": 3, "a3": 1, "b2": 2, "c1": 4}


def _visit_at(key, start, end, cam=4):
    with visitors._conn() as conn:
        conn.execute("INSERT INTO visitor_report_log (event_id, camera_id, camera_name, visit_key, alert_seq, entered_at, alert_at, left_at, last_seen) "
                     "VALUES (?, ?, 'Cam3', ?, 1, ?, ?, ?, ?)", (next(_ids), cam, key, start, start + 30, end, end))


def test_one_visitors_split_rows_are_one_visit(api, admin_token, isolated_db, monkeypatch):
    """The person detector can lose a seated visitor and pick them up as a new person, which opens
    a second row of the same stay: the same visitor's rows that overlap or are under 5 minutes
    apart are one visit, and "came N times" counts separate visits only."""
    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    visitors.init_db()
    face_db.init_face_tables()
    at = lambda h, m, s=0: datetime.datetime(2026, 11, 3, h, m, s).timestamp()  # noqa: E731
    _visit_at("p1", at(11, 40, 10), at(11, 44, 58))      # the same person: rows overlap
    _visit_at("p2", at(11, 43, 29), at(11, 46, 43))
    _visit_at("p3", at(11, 49), at(11, 52))              # 2 min after: still the same visit
    _visit_at("p4", at(12, 30), at(12, 35))              # 38 min later: a new visit
    for t in (at(11, 41), at(11, 45), at(11, 50), at(12, 31)):
        _face(4, t, tier="unknown", embedding=_emb(7))
    rows = api.get("/api/visitors/visits", headers=bearer(admin_token)).json()["visits"]
    assert [(v["visitor_number"], v["visitor_visits"]) for v in rows] == [(1, 2), (1, 2)]
    first = rows[1]
    assert first["arrived_at"] == at(11, 40, 10) and first["last_seen"] == at(11, 52)
    assert first["duration_seconds"] == at(11, 52) - at(11, 40, 10) and first["alerts"] == 3
    assert visitors.kpis(now=at(20, 0), day="2026-11-03")["visitors_today"] == 2


def test_one_visitor_never_shows_twice_at_the_same_time(isolated_db, monkeypatch):
    """Rows of one visitor never overlap, even on two cameras: they are one visit."""
    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    row = lambda k, cam, name, a, b, n: {"visit": k, "camera_id": cam, "camera_name": name, "arrived_at": a, "last_seen": b,  # noqa: E731
                                         "left_at": b, "status": "left", "alerts": 1, "visitor_number": n}
    rows = [row("x", 4, "Cam3", 100, 400, 1), row("y", 5, "CAM4", 300, 700, 1), row("z", 4, "Cam3", 350, 500, 2)]
    out = visitors.merge_same_visitor(rows)
    assert [(r["visitor_number"], r["arrived_at"], r["last_seen"], r["camera_name"]) for r in out] == [(2, 350, 500, "Cam3"), (1, 100, 700, "Cam3, CAM4")]
    ends = {}
    for r in sorted(out, key=lambda r: r["arrived_at"]):
        assert r["arrived_at"] >= ends.get(r["visitor_number"], 0)
        ends[r["visitor_number"]] = r["left_at"]
