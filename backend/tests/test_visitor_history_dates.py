"""Visitor history filtered by date (GET /api/visitors/visits?start=&end=)."""

import datetime

from app import visitors
from tests.conftest import bearer


def _visit(key, day, hour):
    t = datetime.datetime.combine(datetime.date.fromisoformat(day), datetime.time(hour)).timestamp()
    with visitors._conn() as conn:
        conn.execute("INSERT INTO visitor_report_log (event_id, camera_id, camera_name, visit_key, alert_seq, entered_at, alert_at, left_at, last_seen) "
                     "VALUES (?, 4, 'Cam3', ?, 1, ?, ?, ?, ?)", (abs(hash(key)) % 100000, key, t - 60, t, t + 300, t + 300))


def test_visits_by_date_range(api, admin_token, isolated_db):
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
