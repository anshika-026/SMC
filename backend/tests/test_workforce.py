"""Workforce management (workforce.py): policy, holidays, leave/WFH requests
and balances, and the classification of each employee's day from gate
crossings and face sightings."""

import datetime

import pytest

from app import attendance, employee_directory, workforce
from app.staff import occupancy

# A fixed past Wednesday, so "today" logic and weekly offs are predictable.
DAY = "2026-10-21"
MONDAY = "2026-10-19"
SUNDAY = "2026-10-25"
NOW = datetime.datetime(2026, 10, 30, 12, 0).timestamp()   # well after DAY
ADMIN = {"principal_type": "admin", "email": "hr@example.com"}


def at(day, hh, mm=0):
    return datetime.datetime.strptime(f"{day} {hh:02d}:{mm:02d}", "%Y-%m-%d %H:%M").timestamp()


@pytest.fixture
def wf(isolated_db, monkeypatch):
    for mod in (workforce, attendance, occupancy):
        monkeypatch.setattr(mod, "DB_PATH", isolated_db)
    monkeypatch.setattr(employee_directory, "EMPLOYEES", {
        "E1": {"name": "Asha", "company": "Sales"},
        "E2": {"name": "Bala", "company": "Sales"},
        "E3": {"name": "Chitra", "company": "Accounts"},
    })
    monkeypatch.setattr(employee_directory, "get_employee", lambda e: employee_directory.EMPLOYEES.get(e))
    attendance.init_db()
    workforce.init_db()
    workforce.set_policy({"shift_start": "09:00", "grace_minutes": 30, "shift_end": "18:00", "half_day_after": "12:00",
                          "weekly_offs": ["Sunday"], "overtime_after_minutes": 30, "long_outside_minutes": 60}, ADMIN)
    return isolated_db


def seen(emp, day, first, last):
    with attendance._conn() as conn:
        conn.execute("INSERT INTO attendance_days (employee_id, day, first_seen, last_seen, sightings) VALUES (?, ?, ?, ?, 10)",
                     (emp, day, first, last))


def gate(emp, entry, exit_=None, auto=False):
    vid = f"V{emp}{int(entry)}"
    with occupancy._conn() as conn:
        conn.execute("INSERT INTO staff_occupancy (visit_id, employee_id, status, entry_time, exit_time, last_seen) VALUES (?, ?, ?, ?, ?, ?)",
                     (vid, emp, "EXITED" if exit_ else "PRESENT", entry, exit_, exit_ or entry))
        if auto:
            conn.execute("INSERT INTO staff_events (ts, event_type, visit_id, employee_id) VALUES (?, 'AUTO_EXIT', ?, ?)", (exit_, vid, emp))


def day_of(emp, day=DAY, now=NOW):
    return next(r for r in workforce.classify_range(day, day, now=now) if r["employee_id"] == emp)


# ---- policy -----------------------------------------------------------------

def test_policy_is_validated_and_audited(wf):
    with pytest.raises(ValueError):
        workforce.set_policy({"shift_start": "nine"}, ADMIN)
    with pytest.raises(ValueError):
        workforce.set_policy({"weekly_offs": ["Funday"]}, ADMIN)
    with pytest.raises(ValueError):
        workforce.set_policy({"shift_end": "08:00"}, ADMIN)
    with pytest.raises(ValueError):
        workforce.set_policy({"salary": 1}, ADMIN)
    p = workforce.set_policy({"grace_minutes": "15", "weekly_offs": ["Sunday", "Saturday"]}, ADMIN)
    assert p["grace_minutes"] == 15 and p["weekly_offs"] == ["Saturday", "Sunday"]
    from app import audit
    assert any(a["action"] == "attendance.policy_update" for a in audit.recent(50))


# ---- classification ---------------------------------------------------------------

def test_on_time_late_half_day_and_absent(wf):
    seen("E1", DAY, at(DAY, 9, 10), at(DAY, 18, 0))
    seen("E2", DAY, at(DAY, 9, 45), at(DAY, 18, 0))
    seen("E3", DAY, at(DAY, 12, 30), at(DAY, 18, 0))
    assert day_of("E1")["status"] == "Present"
    assert day_of("E2")["status"] == "Late" and day_of("E2")["late"]
    assert day_of("E3")["status"] == "Half day" and day_of("E3")["half_day"]
    seen_none = workforce.classify_range(MONDAY, MONDAY, now=NOW)
    assert {r["status"] for r in seen_none} == {"Absent"}


def test_holidays_and_weekly_offs_are_not_absences(wf):
    workforce.save_holiday(DAY, "Diwali", None, ADMIN)
    assert day_of("E1")["status"] == "Holiday"
    assert {r["status"] for r in workforce.classify_range(SUNDAY, SUNDAY, now=NOW)} == {"Weekly off"}
    with pytest.raises(ValueError):
        workforce.save_holiday(DAY, "Again", None, ADMIN)          # one per day per site


def test_work_on_a_day_off_is_overtime(wf):
    seen("E1", SUNDAY, at(SUNDAY, 10), at(SUNDAY, 14))
    r = day_of("E1", SUNDAY)
    assert r["status"] == "Weekly off" and r["overtime_minutes"] == 240


def test_full_leave_half_day_leave_and_wfh(wf):
    workforce.allocate_defaults(2026, ADMIN)
    workforce.create_request("E1", "leave", DAY, DAY, "full", "CL", "", ADMIN, approve=True)
    workforce.create_request("E2", "leave", DAY, DAY, "first", "SL", "", ADMIN, approve=True)
    workforce.create_request("E3", "wfh", DAY, DAY, "full", None, "", ADMIN, approve=True)
    seen("E2", DAY, at(DAY, 13, 10), at(DAY, 18, 0))   # in for the second half, on time for it
    assert day_of("E1")["status"] == "On leave"
    assert day_of("E2")["status"] == "Half day" and day_of("E2")["half_day_leave"] == "first" and not day_of("E2")["late"]
    assert day_of("E3")["status"] == "WFH"


def test_pending_requests_do_not_change_the_day(wf):
    workforce.create_request("E1", "leave", DAY, DAY, "full", "CL", "", ADMIN)
    assert day_of("E1")["status"] == "Absent"


# ---- gate sessions -----------------------------------------------------------------

def test_temporary_exit_outside_duration_and_final_exit(wf):
    gate("E1", at(DAY, 9, 0), at(DAY, 13, 0))
    gate("E1", at(DAY, 14, 30), at(DAY, 18, 5))         # 90 min out at lunch
    r = day_of("E1")
    assert r["source"] == "gate" and r["status"] == "Present"
    assert r["outside_seconds"] == 90 * 60 and r["outside_periods"] == 1 and r["long_outside"]
    assert r["inside_seconds"] == (4 * 60 + 215) * 60
    assert r["last_out"] == at(DAY, 18, 5) and not r["early_departure"] and not r["missing_exit"]


def test_gate_beats_last_sighting_and_detects_early_departure(wf):
    gate("E1", at(DAY, 9, 0), at(DAY, 16, 0))           # confirmed exit at 16:00
    seen("E1", DAY, at(DAY, 9, 1), at(DAY, 15, 55))
    r = day_of("E1")
    assert r["last_out"] == at(DAY, 16, 0) and r["early_departure"]


def test_end_of_day_closed_exit_is_flagged_missing(wf):
    gate("E1", at(DAY, 9, 0), at(DAY, 23, 59), auto=True)
    r = day_of("E1")
    assert r["missing_exit"]


def test_seen_by_face_but_never_at_the_gate_is_flagged(wf):
    seen("E1", DAY, at(DAY, 9, 5), at(DAY, 18, 0))
    assert day_of("E1")["missing_entry"]


def test_currently_inside_and_currently_outside_today(wf):
    today = "2026-10-30"
    now = at(today, 15, 30)                              # E2 out for 90 min
    gate("E1", at(today, 9, 0))                          # still inside
    gate("E2", at(today, 9, 0), at(today, 14, 0))        # stepped out at 14:00, not back
    assert day_of("E1", today, now)["inside_now"]
    r = day_of("E2", today, now)
    assert r["outside_now"] and not r["inside_now"] and r["long_outside"]


def test_overtime_after_the_shift(wf):
    gate("E1", at(DAY, 9, 0), at(DAY, 20, 0))
    assert day_of("E1")["overtime_minutes"] == 120


# ---- requests and balances ----------------------------------------------------------

def test_duplicate_and_overlapping_requests_are_refused(wf):
    workforce.create_request("E1", "leave", DAY, "2026-10-23", "full", "CL", "", ADMIN)
    with pytest.raises(ValueError):
        workforce.create_request("E1", "wfh", "2026-10-22", "2026-10-22", "full", None, "", ADMIN)
    workforce.create_request("E2", "leave", DAY, DAY, "first", "CL", "", ADMIN)
    workforce.create_request("E2", "wfh", DAY, DAY, "second", None, "", ADMIN)      # the other half is fine
    with pytest.raises(ValueError):
        workforce.create_request("E2", "wfh", DAY, DAY, "first", None, "", ADMIN)


def test_weekly_offs_and_holidays_are_not_counted_as_leave_days(wf):
    workforce.save_holiday("2026-10-23", "Local holiday", None, ADMIN)
    r = workforce.create_request("E1", "leave", "2026-10-21", "2026-10-26", "full", "CL", "", ADMIN)
    assert r["days"] == 4     # Wed 21, Thu 22, Sat 24, Mon 26: not the Fri holiday or Sunday


def test_approval_uses_the_balance(wf):
    workforce.set_allocation("E1", "CL", 2026, allotted=1.5, actor=ADMIN)
    a = workforce.create_request("E1", "leave", DAY, DAY, "full", "CL", "", ADMIN)
    b = workforce.create_request("E1", "leave", "2026-10-22", "2026-10-22", "first", "CL", "", ADMIN)
    c = workforce.create_request("E1", "leave", "2026-10-24", "2026-10-24", "full", "CL", "", ADMIN)
    assert workforce.balance("E1", 2026, "CL")[0]["pending"] == 2.5
    with pytest.raises(ValueError):                 # "approve now" checks the balance too
        workforce.create_request("E2", "leave", DAY, DAY, "full", "CL", "", ADMIN, approve=True)
    workforce.decide_request(a["id"], "approved", "", ADMIN)
    workforce.decide_request(b["id"], "approved", "", ADMIN)
    bal = workforce.balance("E1", 2026, "CL")[0]
    assert bal["used"] == 1.5 and bal["remaining"] == 0 and bal["pending"] == 1
    with pytest.raises(ValueError):
        workforce.decide_request(c["id"], "approved", "", ADMIN)   # not enough left
    workforce.decide_request(c["id"], "rejected", "no balance", ADMIN)
    with pytest.raises(ValueError):
        workforce.decide_request(c["id"], "approved", "", ADMIN)   # already decided


def test_default_allocation_with_capped_carry_forward(wf):
    workforce.set_allocation("E1", "EL", 2025, allotted=40, actor=ADMIN)
    created = workforce.allocate_defaults(2026, ADMIN)
    assert created == 3 * len(workforce.leave_types(active_only=True))
    el = workforce.balance("E1", 2026, "EL")[0]
    assert el["allotted"] == 15 and el["carried_forward"] == 30      # capped at the type's 30
    assert workforce.allocate_defaults(2026, ADMIN) == 0               # never twice


# ---- the overview and its KPI cards ---------------------------------------------

def test_overview_cards_are_filters_and_department_filter_works(wf):
    seen("E1", DAY, at(DAY, 9, 50), at(DAY, 18, 0))      # late
    seen("E2", DAY, at(DAY, 9, 5), at(DAY, 18, 0))
    o = workforce.overview(DAY, now=NOW)
    cards = {c["key"]: c["count"] for c in o["cards"]}
    assert cards["total"] == 3 and cards["late"] == 1 and cards["absent"] == 1
    late_only = workforce.overview(DAY, kpi="late", now=NOW)
    assert [r["name"] for r in late_only["rows"]] == ["Asha"]
    sales = workforce.overview(DAY, department="Sales", now=NOW)
    assert {r["employee_id"] for r in sales["rows"]} == {"E1", "E2"} and sales["departments"] == ["Accounts", "Sales"]


def test_late_allowance_exceeded_over_the_month(wf):
    workforce.set_policy({"late_allowance_per_month": 2}, ADMIN)
    for d in ("2026-10-19", "2026-10-20", "2026-10-21"):
        seen("E1", d, at(d, 9, 45), at(d, 18, 0))
    o = workforce.overview(DAY, kpi="late_limit", now=NOW)
    assert [r["employee_id"] for r in o["rows"]] == ["E1"] and o["rows"][0]["month"]["late"] == 3


# ---- where is everyone now: gate crossings + which camera last saw them ------------

GATE_CAM, INSIDE_CAM = 1, 4


def recognised(emp, cam, first, last):
    with attendance._conn() as conn:
        conn.execute("INSERT INTO face_recognition_events (employee_id, camera_id, first_seen, last_seen, sightings, best_confidence) "
                     "VALUES (?, ?, ?, ?, 2, 0.9)", (emp, cam, first, last))


@pytest.fixture
def gate_cam(wf):
    with occupancy._conn() as conn:
        conn.execute("INSERT INTO staff_camera_config (camera_id, enabled, line, inside_sign) VALUES (?, 1, '[[0,0.5],[1,0.5]]', 1)", (GATE_CAM,))
    return wf


def test_seen_at_the_gate_after_inside_is_likely_outside(gate_cam):
    today = "2026-10-30"
    now = at(today, 14, 43)
    seen("E1", today, at(today, 10, 51), at(today, 14, 42))
    recognised("E1", INSIDE_CAM, at(today, 11, 0), at(today, 12, 58))   # last inside 12:58
    recognised("E1", GATE_CAM, at(today, 13, 19), at(today, 13, 20))    # at the gate, then nowhere since
    r = day_of("E1", today, now)
    assert r["location"] == "likely_outside" and r["outside_now"] and not r["inside_now"] and not r["outside_confirmed"]
    assert "12:58" in r["location_detail"] and r["missing_exit"]
    o = workforce.overview(today, kpi="outside", now=now)
    assert [x["employee_id"] for x in o["rows"]] == ["E1"]


def test_recently_seen_on_an_inside_camera_is_inside(gate_cam):
    today = "2026-10-30"
    now = at(today, 14, 43)
    seen("E1", today, at(today, 10, 0), at(today, 14, 40))
    recognised("E1", GATE_CAM, at(today, 10, 0), at(today, 10, 1))
    recognised("E1", INSIDE_CAM, at(today, 10, 5), at(today, 14, 40))
    r = day_of("E1", today, now)
    assert r["location"] == "inside" and r["inside_now"] and not r["outside_now"]


def test_only_ever_at_the_gate_is_not_called_outside(gate_cam):
    today = "2026-10-30"
    now = at(today, 14, 43)
    seen("E1", today, at(today, 14, 0), at(today, 14, 40))
    recognised("E1", GATE_CAM, at(today, 14, 0), at(today, 14, 5))      # at the gate, then nowhere since
    r = day_of("E1", today, now)
    assert r["location"] == "at_gate" and not r["outside_now"] and not r["inside_now"]


def test_confirmed_exit_then_seen_inside_again_is_inside(gate_cam):
    today = "2026-10-30"
    now = at(today, 15, 0)
    gate("E1", at(today, 9, 0), at(today, 13, 0))                         # left at 13:00
    recognised("E1", INSIDE_CAM, at(today, 14, 0), at(today, 14, 55))    # back without the gate naming them
    r = day_of("E1", today, now)
    assert r["location"] == "inside" and not r["outside_now"]


def test_not_seen_for_longer_than_inside_keep_minutes_is_outside(gate_cam):
    today = "2026-10-30"
    now = at(today, 16, 0)
    seen("E1", today, at(today, 9, 0), at(today, 12, 0))
    recognised("E1", INSIDE_CAM, at(today, 9, 0), at(today, 12, 0))
    r = day_of("E1", today, now)
    assert r["location"] == "likely_outside" and "12:00" in r["location_detail"] and not r["inside_now"] and r["outside_now"]


def test_one_or_two_sightings_are_not_a_presence(api, admin_token, isolated_db, monkeypatch):
    """A single frame of someone else's face matched by mistake must not mark an employee present."""
    from app import attendance

    assert not attendance.confirmed({"sightings": 1}) and not attendance.confirmed({"sightings": 2})
    assert attendance.confirmed({"sightings": attendance.MIN_SIGHTINGS}) and not attendance.confirmed(None)


def test_seen_at_reception_recently_is_inside():
    """The gate camera also sees the reception seating: recognised there recently = inside, not
    'likely outside'; last seen at the gate and then nowhere for longer = likely outside."""
    gate = {1}
    names = {1: "CAM1", 2: "CAM2"}
    now = 10_000.0
    s = [{"camera_id": 2, "first_seen": now - 3000, "last_seen": now - 2000},
         {"camera_id": 1, "first_seen": now - 70, "last_seen": now - 60}]
    loc = workforce._location(now, [], None, s, gate, names)
    assert loc["location"] == "inside" and "reception" in loc["location_detail"]
    later = now + (workforce.GATE_KEEP_MINUTES + 1) * 60
    assert workforce._location(later, [], None, s, gate, names)["location"] == "likely_outside"


def test_last_seen_in_the_hall_counts_as_inside_for_inside_keep_minutes():
    """Recognised now and then in the hall: inside for INSIDE_KEEP_MINUTES, then 'not seen since'."""
    now = 100_000.0
    s = [{"camera_id": 2, "first_seen": now - 1300, "last_seen": now - 1200}]     # 20 min ago in the hall
    assert workforce._location(now, [], None, s, {1}, {2: "CAM2"})["location"] == "inside"
    later = now + workforce.INSIDE_KEEP_MINUTES * 60
    assert workforce._location(later, [], None, s, {1}, {2: "CAM2"})["location"] == "likely_outside"
