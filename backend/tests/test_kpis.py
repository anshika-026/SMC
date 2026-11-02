"""KPI cards: every figure computed from recorded data (attendance days,
staff visits, visitor visits). No sample numbers."""

import datetime
import time

from app import attendance, employee_directory
from app.staff import occupancy


def _day(offset=0):
    return (datetime.date.today() - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")


def _at(day, hh, mm):
    return datetime.datetime.strptime(f"{day} {hh:02d}:{mm:02d}", "%Y-%m-%d %H:%M").timestamp()


def test_attendance_repeat_late_and_seen_once(isolated_db, monkeypatch):
    monkeypatch.setattr(attendance, "DB_PATH", isolated_db)
    monkeypatch.setattr(attendance, "ATTENDANCE_LATE_AFTER", "09:30")
    monkeypatch.setattr(employee_directory, "EMPLOYEES", {
        "E1": {"name": "Late Lata", "company": None},
        "E2": {"name": "Once Om", "company": None},
        "E3": {"name": "Ontime Uma", "company": None},
    })
    attendance.init_db()
    rows = []
    for off in (0, 3, 9):                       # E1 late 3 times in 30 days
        d = _day(off)
        rows.append(("E1", d, _at(d, 10, 5), _at(d, 17, 0)))
    rows.append(("E1", _day(40), _at(_day(40), 10, 0), _at(_day(40), 17, 0)))   # outside the window
    rows.append(("E2", _day(0), _at(_day(0), 9, 0), _at(_day(0), 9, 0)))         # one sighting today
    rows.append(("E3", _day(0), _at(_day(0), 9, 10), _at(_day(0), 18, 0)))
    with attendance._conn() as conn:
        conn.executemany("INSERT INTO attendance_days (employee_id, day, first_seen, last_seen, sightings) VALUES (?, ?, ?, ?, 1)", rows)

    s = attendance.day_report(_day(0), now=_at(_day(0), 19, 0))["stats"]
    assert s["present"] == 3 and s["total"] == 3
    assert s["late"] == 1                       # only E1 today
    assert s["late_repeat"] == 1 and s["late_repeat_threshold"] == 3
    assert s["seen_once"] == 1                  # E2


def test_staff_peak_inside_today(isolated_db, monkeypatch):
    monkeypatch.setattr(occupancy, "DB_PATH", isolated_db)
    m = occupancy.StaffOccupancyManager()
    today = _day(0)
    visits = [  # (entry, exit or None)
        (_at(today, 9, 0), _at(today, 12, 0)),
        (_at(today, 10, 0), _at(today, 11, 0)),
        (_at(today, 10, 30), None),             # still inside
        (_at(today, 13, 0), _at(today, 14, 0)),
        (_at(_day(1), 9, 0), _at(_day(1), 18, 0)),   # yesterday only: not today
    ]
    with occupancy._conn() as conn:
        conn.executemany(
            "INSERT INTO staff_occupancy (visit_id, status, entry_time, exit_time, last_seen) VALUES (?, ?, ?, ?, ?)",
            [(f"V{i}", "EXITED" if out else "PRESENT", inn, out, out or inn) for i, (inn, out) in enumerate(visits)],
        )
    c = m.counts(now=_at(today, 15, 0))
    assert c["peak_inside_today"] == 3          # 10:30-11:00: three people inside at once
    assert c["peak_at"].endswith("10:30")


def test_staff_peak_is_zero_with_nobody(isolated_db, monkeypatch):
    monkeypatch.setattr(occupancy, "DB_PATH", isolated_db)
    c = occupancy.StaffOccupancyManager().counts(now=time.time())
    assert c["peak_inside_today"] == 0 and c["peak_at"] is None


def test_visits_left_open_from_an_earlier_day_are_closed(isolated_db, monkeypatch):
    monkeypatch.setattr(occupancy, "DB_PATH", isolated_db)
    m = occupancy.StaffOccupancyManager()
    yesterday, today = _day(1), _day(0)
    m.entry(1, 7, ts=_at(yesterday, 14, 28))          # never left: backend off at 23:59
    m.entry(1, 8, ts=_at(today, 9, 0))                # really inside today
    assert m.counts(now=_at(today, 10, 0))["total_persons"] == 2
    assert m.maybe_end_of_day(now=_at(today, 10, 0)) == 1
    c = m.counts(now=_at(today, 10, 0))
    assert c["total_persons"] == 1 and c["total_exits_today"] == 0   # closed on yesterday, not today
    with occupancy._conn() as conn:
        row = conn.execute("SELECT exit_time FROM staff_occupancy WHERE status = 'EXITED'").fetchone()
    assert _at(yesterday, 23, 59) <= row[0] < _at(today, 0, 0)


def test_business_overview_handles_an_empty_day(isolated_db, monkeypatch):
    from app import insights, visitors
    from app.reid import reid_db

    for mod in (insights, visitors, attendance):
        monkeypatch.setattr(mod, "DB_PATH", isolated_db)
    monkeypatch.setattr(reid_db, "DB_PATH", isolated_db)
    monkeypatch.setattr(employee_directory, "EMPLOYEES", {"E1": {"name": "Asha", "company": None}})
    attendance.init_db()
    visitors.init_db()
    reid_db.init_db()
    from app import alerts
    monkeypatch.setattr(alerts, "DB_PATH", isolated_db)
    alerts.init_db()

    from app import presence, visitor_registry, workforce
    for mod in (presence, visitor_registry, workforce):
        monkeypatch.setattr(mod, "DB_PATH", isolated_db)
    workforce.init_db()
    presence.init_db()
    visitor_registry.init_db()
    monkeypatch.setattr(employee_directory, "get_employee", lambda e: employee_directory.EMPLOYEES.get(e))

    b = insights.business_overview()
    w = b["workforce"]
    assert b["is_today"] and w["present"] == 0 and w["total"] == 1 and w["attendance_pct"] == 0
    assert b["visitors"]["visitors"] == 0 and b["visitors"]["waiting_now"] == 0 and b["visitors"]["average_waiting_seconds"] is None
    assert b["footfall"]["entries"] == 0 and b["footfall"]["peak_hour"] is None
    s = b["security"]
    assert s["active"] == 0 and s["incidents"] == [] and s["intrusion"] is None     # not supported: unavailable, not zero
    assert isinstance(b["attention"], list) and [x["area"] for x in b["summary"]][-1] == "Security"

    past = insights.business_overview(_day(5))
    assert not past["is_today"] and past["understaffed"] is None and past["areas"] is None
    assert past["visitors"]["inside_now"] is None and past["visitors"]["waiting_now"] is None
    if past["staff"]:
        assert past["staff"]["inside_now"] is None    # live figures only for today
