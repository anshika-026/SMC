"""
workforce.py

Workforce management on top of attendance.py: the attendance POLICY (shift,
grace, half-day cut-off, monthly allowances, weekly offs, overtime and payroll
rules), the HOLIDAY calendar, LEAVE types, allocations and balances, LEAVE and
WORK-FROM-HOME requests with approval, ENTRY/EXIT sessions from confirmed gate
crossings (Staff Count, staff/occupancy.py), and the CLASSIFICATION of every
employee's day that the Attendance page, its KPI cards and the monthly figures
are built from.

Everything comes from recorded data and the stored policy; nothing is a sample.
Requests are entered and decided by HR/admins (employees have no login yet).
Every policy change and request decision is written to the audit log.

Sources, in order of authority for a day:
  holiday / weekly off   -> the policy and holidays tables
  approved leave / WFH   -> leave_requests (and the older attendance_leave rows)
  in / out times         -> confirmed gate crossings (staff_occupancy visits of
                            that employee); face sightings (attendance_days)
                            when the gate never identified them
  manual corrections     -> attendance_corrections: an admin's entry / exit
                            time or "presence verified" (with a reason), applied
                            on top; the recorded times stay as they were and
                            every correction (and its withdrawal) is kept
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time

from app import attendance, audit, config, db, employee_directory

DB_PATH = config.DB_PATH

# Statuses an employee's day can have (classify()).
HOLIDAY, WEEKLY_OFF, ON_LEAVE, WFH = "Holiday", "Weekly off", "On leave", "WFH"
PRESENT, LATE, HALF_DAY, ABSENT, NOT_YET = "Present", "Late", "Half day", "Absent", "Not in yet"
WORKED = (PRESENT, LATE, HALF_DAY)

DEFAULT_LEAVE_TYPES = [
    # code, name, days a year, carry forward up to, expires after (months, 0 = never)
    ("CL", "Casual Leave", 12, 0, 0),
    ("SL", "Sick Leave", 12, 0, 0),
    ("EL", "Earned Leave", 15, 30, 0),
    ("OL", "Optional Leave", 2, 0, 12),
]


def _conn():
    return db.connect(DB_PATH, row_factory=sqlite3.Row)


def _hm_to_minutes(hm: str) -> int:
    h, m = (int(x) for x in hm.split(":"))
    return h * 60 + m


def _minutes_to_hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# --------------------------------------------------------------------------- policy

def default_policy() -> dict:
    """Defaults that reproduce how attendance worked before the policy existed
    (ATTENDANCE_LATE_AFTER, ATTENDANCE_SHIFT_END, ...), so nothing changes
    until an admin edits it."""
    shift_start = "09:00"
    grace = max(0, _hm_to_minutes(attendance.ATTENDANCE_LATE_AFTER) - _hm_to_minutes(shift_start))
    return {
        "shift_start": shift_start,
        "shift_end": attendance.ATTENDANCE_SHIFT_END,
        "grace_minutes": grace,                      # late = first in after shift_start + grace
        "half_day_after": "12:00",                   # first in after this = half day
        "early_departure_grace_minutes": int(attendance.ATTENDANCE_EARLY_GRACE_MINUTES),
        "weekly_offs": ["Sunday"],
        "late_allowance_per_month": attendance.LATE_REPEAT_COUNT,
        "half_day_allowance_per_month": 2,
        "long_outside_minutes": 60,                  # one outside period longer than this is flagged
        "overtime_after_minutes": int(attendance.ATTENDANCE_OVERTIME_MINUTES),
        "overtime_rate": 1.5,                        # multiple of the hourly rate
        # Payroll preview only - nothing is ever deducted automatically.
        "deduct_days_per_late_beyond_allowance": 0.0,   # e.g. 0.5 = half a day per extra late
        "deduct_days_per_absent": 1.0,
        "deduct_days_per_half_day": 0.5,
        "daily_rate": 0.0,                           # 0 = show days, not money
    }


POLICY_TYPES = {
    "shift_start": "hm", "shift_end": "hm", "half_day_after": "hm",
    "grace_minutes": int, "early_departure_grace_minutes": int, "late_allowance_per_month": int,
    "half_day_allowance_per_month": int, "long_outside_minutes": int, "overtime_after_minutes": int,
    "overtime_rate": float, "deduct_days_per_late_beyond_allowance": float, "deduct_days_per_absent": float,
    "deduct_days_per_half_day": float, "daily_rate": float, "weekly_offs": list,
}
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def init_db() -> None:
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS attendance_policy (id INTEGER PRIMARY KEY CHECK (id = 1), "
                     "policy TEXT NOT NULL, updated_at REAL, updated_by TEXT)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS holidays (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   day TEXT NOT NULL,              -- YYYY-MM-DD
                   name TEXT NOT NULL,
                   site TEXT,                      -- NULL = whole organisation
                   created_at REAL NOT NULL,
                   created_by TEXT,
                   UNIQUE (day, site))"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS leave_types (
                   code TEXT PRIMARY KEY,
                   name TEXT NOT NULL,
                   annual_days REAL NOT NULL DEFAULT 0,
                   carry_forward_max REAL NOT NULL DEFAULT 0,
                   expires_months INTEGER NOT NULL DEFAULT 0,
                   active INTEGER NOT NULL DEFAULT 1)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS leave_allocations (
                   employee_id TEXT NOT NULL,
                   leave_type TEXT NOT NULL,
                   year INTEGER NOT NULL,
                   allotted REAL NOT NULL DEFAULT 0,
                   carried_forward REAL NOT NULL DEFAULT 0,
                   adjustment REAL NOT NULL DEFAULT 0,
                   note TEXT,
                   updated_at REAL,
                   PRIMARY KEY (employee_id, leave_type, year))"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS leave_requests (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   employee_id TEXT NOT NULL,
                   kind TEXT NOT NULL,             -- leave | wfh
                   leave_type TEXT,                -- leave only
                   day_from TEXT NOT NULL,
                   day_to TEXT NOT NULL,
                   half TEXT NOT NULL DEFAULT 'full',   -- full | first | second
                   days REAL NOT NULL,             -- working days it covers (0.5 for a half day)
                   reason TEXT,
                   status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected | cancelled
                   requested_at REAL NOT NULL,
                   requested_by TEXT,
                   decided_at REAL,
                   decided_by TEXT,
                   decision_note TEXT)"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leave_requests_emp ON leave_requests (employee_id, day_from)")
        # Manual attendance corrections. Never deleted: a withdrawn one is
        # marked voided (who, when, why), so the full history stays.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS attendance_corrections (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   employee_id TEXT NOT NULL,
                   day TEXT NOT NULL,              -- YYYY-MM-DD
                   kind TEXT NOT NULL,             -- entry | exit | presence_verified
                   ts REAL NOT NULL,               -- the corrected time / when they were seen present
                   reason TEXT NOT NULL,
                   evidence TEXT,                  -- e.g. "recognised on CAM2 at 10:42 AM"
                   original_ts REAL,               -- what the record said before (entry / exit)
                   created_at REAL NOT NULL,
                   created_by TEXT,
                   voided INTEGER NOT NULL DEFAULT 0,
                   voided_at REAL,
                   voided_by TEXT,
                   void_reason TEXT)"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_attendance_corrections ON attendance_corrections (employee_id, day)")
        if conn.execute("SELECT COUNT(*) FROM leave_types").fetchone()[0] == 0:
            conn.executemany("INSERT INTO leave_types (code, name, annual_days, carry_forward_max, expires_months) VALUES (?, ?, ?, ?, ?)",
                             DEFAULT_LEAVE_TYPES)


def get_policy() -> dict:
    with _conn() as conn:
        row = conn.execute("SELECT policy FROM attendance_policy WHERE id = 1").fetchone()
    policy = default_policy()
    if row:
        policy.update({k: v for k, v in json.loads(row["policy"]).items() if k in POLICY_TYPES})
    return policy


def set_policy(changes: dict, actor: dict | None = None) -> dict:
    """Validates and stores policy changes (only known keys); audited."""
    policy = get_policy()
    clean = {}
    for key, value in changes.items():
        kind = POLICY_TYPES.get(key)
        if kind is None:
            raise ValueError(f"Unknown policy setting: {key}")
        if kind == "hm":
            try:
                _hm_to_minutes(str(value))
            except ValueError:
                raise ValueError(f"{key} must be a time like 09:30")
            clean[key] = _minutes_to_hm(_hm_to_minutes(str(value)))
        elif kind is list:
            if not isinstance(value, list) or any(d not in DAYS for d in value):
                raise ValueError("weekly_offs must be a list of day names like Sunday")
            clean[key] = [d for d in DAYS if d in value]
        else:
            try:
                clean[key] = kind(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a number")
            if clean[key] < 0:
                raise ValueError(f"{key} can't be negative")
    policy.update(clean)
    if _hm_to_minutes(policy["shift_end"]) <= _hm_to_minutes(policy["shift_start"]):
        raise ValueError("The shift must end after it starts")
    with _conn() as conn:
        conn.execute("INSERT INTO attendance_policy (id, policy, updated_at, updated_by) VALUES (1, ?, ?, ?) "
                     "ON CONFLICT(id) DO UPDATE SET policy = excluded.policy, updated_at = excluded.updated_at, updated_by = excluded.updated_by",
                     (json.dumps(policy), time.time(), (actor or {}).get("email")))
    audit.record("attendance.policy_update", actor, target="policy", changes=clean)
    return policy


# --------------------------------------------------------------------------- holidays

def list_holidays(year: int | None = None) -> list[dict]:
    q, p = "SELECT * FROM holidays", []
    if year:
        q, p = q + " WHERE day LIKE ?", [f"{year}-%"]
    with _conn() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY day", p)]


def upcoming_holidays(today: str, days: int = 60) -> list[dict]:
    end = (datetime.date.fromisoformat(today) + datetime.timedelta(days=days)).isoformat()
    with _conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM holidays WHERE day >= ? AND day <= ? ORDER BY day", (today, end))]


def save_holiday(day: str, name: str, site: str | None, actor: dict | None = None, holiday_id: int | None = None) -> dict:
    datetime.date.fromisoformat(day)
    name = (name or "").strip()
    if not name:
        raise ValueError("Give the holiday a name")
    site = (site or "").strip() or None
    with _conn() as conn:
        # SQLite's UNIQUE treats NULL sites as all different: check here.
        clash = conn.execute("SELECT id FROM holidays WHERE day = ? AND site IS ? AND id IS NOT ?", (day, site, holiday_id)).fetchone()
    if clash:
        raise ValueError("There is already a holiday on that date" + (f" for {site}" if site else ""))
    try:
        with _conn() as conn:
            if holiday_id is None:
                cur = conn.execute("INSERT INTO holidays (day, name, site, created_at, created_by) VALUES (?, ?, ?, ?, ?)",
                                   (day, name, site, time.time(), (actor or {}).get("email")))
                holiday_id = cur.lastrowid
            elif conn.execute("UPDATE holidays SET day = ?, name = ?, site = ? WHERE id = ?", (day, name, site, holiday_id)).rowcount == 0:
                raise ValueError("No such holiday")
    except sqlite3.IntegrityError:
        raise ValueError("There is already a holiday on that date" + (f" for {site}" if site else ""))
    audit.record("attendance.holiday_save", actor, target=f"holiday:{holiday_id}", day=day, name=name, site=site)
    with _conn() as conn:
        return dict(conn.execute("SELECT * FROM holidays WHERE id = ?", (holiday_id,)).fetchone())


def delete_holiday(holiday_id: int, actor: dict | None = None) -> bool:
    with _conn() as conn:
        ok = conn.execute("DELETE FROM holidays WHERE id = ?", (holiday_id,)).rowcount > 0
    if ok:
        audit.record("attendance.holiday_delete", actor, target=f"holiday:{holiday_id}")
    return ok


def _holidays_between(start: str, end: str) -> dict[str, list[dict]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM holidays WHERE day >= ? AND day <= ?", (start, end)).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["day"], []).append(dict(r))
    return out


# --------------------------------------------------------------------------- leave types and balances

def leave_types(active_only: bool = False) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM leave_types" + (" WHERE active = 1" if active_only else "") + " ORDER BY code").fetchall()
    return [dict(r) for r in rows]


def save_leave_type(code: str, name: str, annual_days: float, carry_forward_max: float = 0, expires_months: int = 0,
                    active: bool = True, actor: dict | None = None) -> dict:
    code = (code or "").strip().upper()
    if not code or not (name or "").strip():
        raise ValueError("A leave type needs a code and a name")
    if annual_days < 0 or carry_forward_max < 0 or expires_months < 0:
        raise ValueError("Days and months can't be negative")
    with _conn() as conn:
        conn.execute("INSERT INTO leave_types (code, name, annual_days, carry_forward_max, expires_months, active) VALUES (?, ?, ?, ?, ?, ?) "
                     "ON CONFLICT(code) DO UPDATE SET name = excluded.name, annual_days = excluded.annual_days, "
                     "carry_forward_max = excluded.carry_forward_max, expires_months = excluded.expires_months, active = excluded.active",
                     (code, name.strip(), annual_days, carry_forward_max, int(expires_months), 1 if active else 0))
    audit.record("attendance.leave_type_save", actor, target=f"leave_type:{code}", annual_days=annual_days,
                 carry_forward_max=carry_forward_max, expires_months=expires_months, active=active)
    return next(t for t in leave_types() if t["code"] == code)


def set_allocation(employee_id: str, leave_type: str, year: int, allotted: float | None = None,
                   carried_forward: float | None = None, adjustment: float | None = None, note: str | None = None,
                   actor: dict | None = None) -> dict:
    if employee_directory.get_employee(employee_id) is None:
        raise ValueError(f"Unknown employee {employee_id}")
    if leave_type not in {t["code"] for t in leave_types()}:
        raise ValueError(f"Unknown leave type {leave_type}")
    current = _allocation_row(employee_id, leave_type, year)
    values = {"allotted": current["allotted"], "carried_forward": current["carried_forward"], "adjustment": current["adjustment"]}
    for key, val in (("allotted", allotted), ("carried_forward", carried_forward), ("adjustment", adjustment)):
        if val is not None:
            values[key] = float(val)
    if values["allotted"] < 0 or values["carried_forward"] < 0:
        raise ValueError("Allotted and carried-forward days can't be negative")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO leave_allocations (employee_id, leave_type, year, allotted, carried_forward, adjustment, note, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(employee_id, leave_type, year) DO UPDATE SET allotted = excluded.allotted, "
            "carried_forward = excluded.carried_forward, adjustment = excluded.adjustment, note = excluded.note, updated_at = excluded.updated_at",
            (employee_id, leave_type, year, values["allotted"], values["carried_forward"], values["adjustment"], note, time.time()),
        )
    audit.record("attendance.leave_allocation", actor, target=f"employee:{employee_id}", leave_type=leave_type, year=year, **values, note=note)
    return balance(employee_id, year, leave_type)[0]


def allocate_defaults(year: int, actor: dict | None = None) -> int:
    """Gives every employee without an allocation for `year` their leave
    types' annual days, plus carry-forward from last year's remaining (capped
    per type). Existing allocations are never changed. Returns rows created."""
    created = 0
    types = leave_types(active_only=True)
    for emp_id in employee_directory.EMPLOYEES:
        for t in types:
            with _conn() as conn:
                exists = conn.execute("SELECT 1 FROM leave_allocations WHERE employee_id = ? AND leave_type = ? AND year = ?",
                                      (emp_id, t["code"], year)).fetchone()
            if exists:
                continue
            carry = 0.0
            if t["carry_forward_max"] > 0:
                prev = balance(emp_id, year - 1, t["code"])[0]
                carry = min(max(prev["remaining"], 0.0), t["carry_forward_max"])
            with _conn() as conn:
                conn.execute("INSERT INTO leave_allocations (employee_id, leave_type, year, allotted, carried_forward, adjustment, note, updated_at) "
                             "VALUES (?, ?, ?, ?, ?, 0, 'default allocation', ?)", (emp_id, t["code"], year, t["annual_days"], carry, time.time()))
            created += 1
    if created:
        audit.record("attendance.leave_allocate_defaults", actor, target=f"year:{year}", rows=created)
    return created


def _allocation_row(employee_id: str, leave_type: str, year: int) -> dict:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM leave_allocations WHERE employee_id = ? AND leave_type = ? AND year = ?",
                         (employee_id, leave_type, year)).fetchone()
    return dict(r) if r else {"allotted": 0.0, "carried_forward": 0.0, "adjustment": 0.0, "note": None}


def balance(employee_id: str, year: int, leave_type: str | None = None) -> list[dict]:
    """Per leave type: allotted (+ carried forward + adjustment), used
    (approved), pending, remaining. Carried-forward days of a type with an
    expiry lapse after that many months into the year."""
    out = []
    today = datetime.date.today()
    for t in leave_types():
        if leave_type and t["code"] != leave_type:
            continue
        a = _allocation_row(employee_id, t["code"], year)
        carried = a["carried_forward"]
        if t["expires_months"] and today.year == year and today.month > t["expires_months"]:
            carried = 0.0
        with _conn() as conn:
            used = conn.execute("SELECT COALESCE(SUM(days), 0) FROM leave_requests WHERE employee_id = ? AND kind = 'leave' AND leave_type = ? "
                                "AND status = 'approved' AND day_from LIKE ?", (employee_id, t["code"], f"{year}-%")).fetchone()[0]
            pending = conn.execute("SELECT COALESCE(SUM(days), 0) FROM leave_requests WHERE employee_id = ? AND kind = 'leave' AND leave_type = ? "
                                   "AND status = 'pending' AND day_from LIKE ?", (employee_id, t["code"], f"{year}-%")).fetchone()[0]
        total = a["allotted"] + carried + a["adjustment"]
        out.append({"leave_type": t["code"], "name": t["name"], "allotted": a["allotted"], "carried_forward": carried,
                    "adjustment": a["adjustment"], "total": total, "used": used, "pending": pending,
                    "remaining": total - used, "note": a.get("note")})
    return out


# --------------------------------------------------------------------------- requests

def _working_days(day_from: str, day_to: str, policy: dict, site: str | None = None) -> list[str]:
    start, end = datetime.date.fromisoformat(day_from), datetime.date.fromisoformat(day_to)
    holidays = _holidays_between(day_from, day_to)
    out, d = [], start
    while d <= end:
        iso = d.isoformat()
        is_holiday = any(h["site"] in (None, site) for h in holidays.get(iso, []))
        if DAYS[d.weekday()] not in policy["weekly_offs"] and not is_holiday:
            out.append(iso)
        d += datetime.timedelta(days=1)
    return out


def create_request(employee_id: str, kind: str, day_from: str, day_to: str, half: str = "full",
                   leave_type: str | None = None, reason: str = "", actor: dict | None = None,
                   approve: bool = False) -> dict:
    """A leave or WFH request entered by HR/admin. Half days are single-day
    requests. Overlapping an existing pending/approved request is refused (no
    duplicates). approve=True records it as approved straight away."""
    if kind not in ("leave", "wfh"):
        raise ValueError("kind must be leave or wfh")
    if employee_directory.get_employee(employee_id) is None:
        raise ValueError(f"Unknown employee {employee_id}")
    datetime.date.fromisoformat(day_from)
    datetime.date.fromisoformat(day_to)
    if day_to < day_from:
        raise ValueError("The request must end on or after the day it starts")
    if half not in ("full", "first", "second"):
        raise ValueError("half must be full, first or second")
    if half != "full" and day_from != day_to:
        raise ValueError("A half-day request is for a single day")
    if kind == "leave":
        if leave_type not in {t["code"] for t in leave_types(active_only=True)}:
            raise ValueError("Choose a leave type")
    else:
        leave_type = None
    policy = get_policy()
    days_list = _working_days(day_from, day_to, policy)
    if not days_list:
        raise ValueError("Those dates are all holidays or weekly offs")
    days = 0.5 if half != "full" else float(len(days_list))
    with _conn() as conn:
        clash = conn.execute(
            "SELECT id, kind, half FROM leave_requests WHERE employee_id = ? AND status IN ('pending', 'approved') "
            "AND day_from <= ? AND day_to >= ?", (employee_id, day_to, day_from)).fetchall()
    for c in clash:
        # Two different halves of the same day may coexist (e.g. leave + WFH).
        if not (half != "full" and c["half"] not in ("full", half)):
            raise ValueError(f"It overlaps request #{c['id']} ({c['kind']})")
    if approve and kind == "leave":
        b = balance(employee_id, int(day_from[:4]), leave_type)[0]
        if b["remaining"] < days:
            raise ValueError(f"Not enough {b['name']}: {b['remaining']:g} day(s) left, {days:g} requested")
    now = time.time()
    status = "approved" if approve else "pending"
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO leave_requests (employee_id, kind, leave_type, day_from, day_to, half, days, reason, status, requested_at, "
            "requested_by, decided_at, decided_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (employee_id, kind, leave_type, day_from, day_to, half, days, reason.strip(), status, now,
             (actor or {}).get("email"), now if approve else None, (actor or {}).get("email") if approve else None))
        rid = cur.lastrowid
    audit.record("attendance.request_create", actor, target=f"request:{rid}", employee=employee_id, kind=kind,
                 leave_type=leave_type, day_from=day_from, day_to=day_to, half=half, status=status)
    return get_request(rid)


def decide_request(request_id: int, decision: str, note: str = "", actor: dict | None = None) -> dict:
    if decision not in ("approved", "rejected", "cancelled"):
        raise ValueError("decision must be approved, rejected or cancelled")
    r = get_request(request_id)
    if r is None:
        raise LookupError("No such request")
    if decision in ("approved", "rejected") and r["status"] != "pending":
        raise ValueError(f"This request is already {r['status']}")
    if decision == "cancelled" and r["status"] not in ("pending", "approved"):
        raise ValueError(f"This request is already {r['status']}")
    if decision == "approved" and r["kind"] == "leave":
        year = int(r["day_from"][:4])
        b = balance(r["employee_id"], year, r["leave_type"])[0]
        if b["remaining"] < r["days"]:
            raise ValueError(f"Not enough {b['name']}: {b['remaining']:g} day(s) left, {r['days']:g} requested")
    with _conn() as conn:
        conn.execute("UPDATE leave_requests SET status = ?, decided_at = ?, decided_by = ?, decision_note = ? WHERE id = ?",
                     (decision, time.time(), (actor or {}).get("email"), note.strip() or None, request_id))
    audit.record("attendance.request_" + decision, actor, target=f"request:{request_id}", employee=r["employee_id"], note=note)
    return get_request(request_id)


def get_request(request_id: int) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM leave_requests WHERE id = ?", (request_id,)).fetchone()
    return _request_view(dict(r)) if r else None


def list_requests(status: str | None = None, employee_id: str | None = None, kind: str | None = None,
                  day_from: str | None = None, day_to: str | None = None, limit: int = 500) -> list[dict]:
    q, p = "SELECT * FROM leave_requests WHERE 1=1", []
    for col, val in (("status", status), ("employee_id", employee_id), ("kind", kind)):
        if val:
            q, p = q + f" AND {col} = ?", p + [val]
    if day_from:
        q, p = q + " AND day_to >= ?", p + [day_from]
    if day_to:
        q, p = q + " AND day_from <= ?", p + [day_to]
    with _conn() as conn:
        rows = conn.execute(q + " ORDER BY requested_at DESC LIMIT ?", (*p, limit)).fetchall()
    return [_request_view(dict(r)) for r in rows]


def _request_view(r: dict) -> dict:
    emp = employee_directory.get_employee(r["employee_id"]) or {}
    r["employee_name"] = emp.get("name") or r["employee_id"]
    return r


def _approved_between(start: str, end: str) -> dict[tuple[str, str], list[dict]]:
    """(employee, day) -> approved requests covering it (plus the older
    attendance_leave rows, read as approved full-day leave)."""
    out: dict[tuple[str, str], list[dict]] = {}
    with _conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM leave_requests WHERE status = 'approved' AND day_from <= ? AND day_to >= ?", (end, start))]
        rows += [{"employee_id": r["employee_id"], "kind": "leave", "leave_type": None, "day_from": r["day_from"],
                  "day_to": r["day_to"], "half": "full", "reason": r["reason"]}
                 for r in conn.execute("SELECT * FROM attendance_leave WHERE day_from <= ? AND day_to >= ?", (end, start))]
    for r in rows:
        d = max(datetime.date.fromisoformat(r["day_from"]), datetime.date.fromisoformat(start))
        last = min(datetime.date.fromisoformat(r["day_to"]), datetime.date.fromisoformat(end))
        while d <= last:
            out.setdefault((r["employee_id"], d.isoformat()), []).append(r)
            d += datetime.timedelta(days=1)
    return out


# --------------------------------------------------------------------------- gate sessions

def _sessions_between(start_ts: float, end_ts: float) -> dict[tuple[str, str], list[dict]]:
    """(employee, day) -> that employee's on-premises sessions from confirmed
    gate crossings (Staff Count visits that face recognition identified)."""
    out: dict[tuple[str, str], list[dict]] = {}
    with _conn() as conn:
        try:
            rows = conn.execute(
                "SELECT o.employee_id, o.entry_time, o.exit_time, o.status, o.camera_id, "
                "EXISTS (SELECT 1 FROM staff_events e WHERE e.visit_id = o.visit_id AND e.event_type = 'AUTO_EXIT') AS auto_exit "
                "FROM staff_occupancy o WHERE o.employee_id IS NOT NULL AND o.entry_time < ? "
                "AND (o.exit_time IS NULL OR o.exit_time >= ?) ORDER BY o.entry_time",
                (end_ts, start_ts)).fetchall()
        except sqlite3.OperationalError:
            return out   # Staff Count never set up: no gate data
    for r in rows:
        day = datetime.date.fromtimestamp(r["entry_time"]).isoformat()
        out.setdefault((r["employee_id"], day), []).append({
            "entry": r["entry_time"], "exit": r["exit_time"], "inside": r["status"] == "PRESENT",
            # Closed by end-of-day reconciliation, not by a seen exit: unconfirmed.
            "exit_confirmed": r["exit_time"] is not None and not r["auto_exit"],
            "camera_id": r["camera_id"],
        })
    return out


# --------------------------------------------------------------------------- manual corrections

CORRECTION_KINDS = {"entry": "Entry time", "exit": "Exit time", "presence_verified": "Presence verified"}


def _correction_view(r) -> dict:
    d = dict(r)
    d["voided"] = bool(d["voided"])
    d["label"] = CORRECTION_KINDS.get(d["kind"], d["kind"])
    return d


def list_corrections(employee_id: str, day: str | None = None) -> list[dict]:
    """Every correction for the employee (that day, if given), withdrawn ones
    included, oldest first: the audit history."""
    q, params = "SELECT * FROM attendance_corrections WHERE employee_id = ?", [employee_id]
    if day:
        q += " AND day = ?"
        params.append(day)
    with _conn() as conn:
        return [_correction_view(r) for r in conn.execute(q + " ORDER BY created_at, id", params)]


def _active_corrections(day_from: str, day_to: str) -> dict[tuple[str, str], list[dict]]:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM attendance_corrections WHERE voided = 0 AND day >= ? AND day <= ? ORDER BY created_at, id",
                            (day_from, day_to)).fetchall()
    out: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        out.setdefault((r["employee_id"], r["day"]), []).append(_correction_view(r))
    return out


def add_correction(employee_id: str, day: str, kind: str, ts: float, reason: str, evidence: str | None = None,
                   actor: dict | None = None, now: float | None = None) -> dict:
    """An admin's correction of one employee's day. Camera evidence is only
    ever attached as context; nothing is corrected without this explicit call."""
    now = now if now is not None else time.time()
    if employee_directory.get_employee(employee_id) is None:
        raise ValueError("Employee not found.")
    if kind not in CORRECTION_KINDS:
        raise ValueError("Correction must be an entry time, an exit time or presence verified.")
    datetime.date.fromisoformat(day)
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Give the reason for the correction.")
    ts = float(ts)
    start = _at(day, "00:00")
    if not start <= ts < start + 86400:
        raise ValueError("The time must be on the same day.")
    if ts > now + 60:
        raise ValueError("The time cannot be in the future.")
    current = classify_range(day, day, employee_ids=[employee_id], now=now)[0]
    if kind == "exit" and current["first_in"] and ts < current["first_in"]:
        raise ValueError("The exit cannot be before the entry.")
    if kind == "entry" and current["last_out"] and ts > current["last_out"] and not current["inside_now"]:
        raise ValueError("The entry cannot be after the exit.")
    original = current.get("original_first_in") if kind == "entry" else current.get("original_last_out") if kind == "exit" else None
    with _conn() as conn:
        # One active correction per kind and day: a new one replaces (voids) the old.
        conn.execute("UPDATE attendance_corrections SET voided = 1, voided_at = ?, voided_by = ?, void_reason = 'replaced by a newer correction' "
                     "WHERE employee_id = ? AND day = ? AND kind = ? AND voided = 0",
                     (now, audit._actor(actor), employee_id, day, kind))
        cur = conn.execute(
            "INSERT INTO attendance_corrections (employee_id, day, kind, ts, reason, evidence, original_ts, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (employee_id, day, kind, ts, reason[:500], (evidence or "").strip()[:500] or None, original, now, audit._actor(actor)))
        cid = cur.lastrowid
    audit.record("attendance.correction_added", actor, f"employee:{employee_id}", day=day, kind=kind, ts=ts, reason=reason,
                 evidence=evidence, original_ts=original, correction_id=cid)
    return {"correction_id": cid, "row": classify_range(day, day, employee_ids=[employee_id], now=now)[0]}


def void_correction(correction_id: int, reason: str, actor: dict | None = None, now: float | None = None) -> dict:
    now = now if now is not None else time.time()
    reason = (reason or "").strip()
    if len(reason) < 3:
        raise ValueError("Give the reason for withdrawing the correction.")
    with _conn() as conn:
        r = conn.execute("SELECT * FROM attendance_corrections WHERE id = ?", (correction_id,)).fetchone()
        if r is None:
            raise KeyError(correction_id)
        if r["voided"]:
            raise ValueError("This correction was already withdrawn.")
        conn.execute("UPDATE attendance_corrections SET voided = 1, voided_at = ?, voided_by = ?, void_reason = ? WHERE id = ?",
                     (now, audit._actor(actor), reason[:500], correction_id))
    audit.record("attendance.correction_withdrawn", actor, f"employee:{r['employee_id']}", day=r["day"], kind=r["kind"],
                 correction_id=correction_id, reason=reason)
    return {"row": classify_range(r["day"], r["day"], employee_ids=[r["employee_id"]], now=now)[0]}


def _apply_corrections(first_in, last_out, corrections: list[dict]) -> tuple:
    """(first_in, last_out, applied) with the active corrections on top of
    the recorded times. The latest correction of each kind wins."""
    latest = {}
    for c in corrections or []:
        latest[c["kind"]] = c
    if "entry" in latest:
        first_in = latest["entry"]["ts"]
    elif "presence_verified" in latest and first_in is None:
        first_in = latest["presence_verified"]["ts"]   # verified present at that time; no earlier record
    if "exit" in latest:
        last_out = latest["exit"]["ts"]
    return first_in, last_out, latest


# --------------------------------------------------------------------------- classification

def _sightings_between(start_ts: float, end_ts: float) -> dict[tuple[str, str], list[dict]]:
    """(employee, day) -> face recognition events (camera, first, last seen):
    WHERE someone was recognised, not just when (attendance.py)."""
    out: dict[tuple[str, str], list[dict]] = {}
    with _conn() as conn:
        try:
            rows = conn.execute("SELECT employee_id, camera_id, first_seen, last_seen FROM face_recognition_events "
                                "WHERE last_seen >= ? AND first_seen < ?", (start_ts, end_ts)).fetchall()
        except sqlite3.OperationalError:
            return out
    for r in rows:
        day = datetime.date.fromtimestamp(r["last_seen"]).isoformat()
        out.setdefault((r["employee_id"], day), []).append(dict(r))
    return out


def _gate_cameras() -> set[int]:
    """Cameras with the main gate line (Staff Count). They also see people
    standing OUTSIDE, so a sighting there doesn't mean someone is inside."""
    with _conn() as conn:
        try:
            return {r[0] for r in conn.execute("SELECT camera_id FROM staff_camera_config WHERE enabled = 1 AND line IS NOT NULL")}
        except sqlite3.OperationalError:
            return set()


# How long after leaving an inside camera's view someone still counts as
# "inside" without a gate crossing, and the gap that makes the gate camera's
# sighting the newer evidence.
SEEN_INSIDE_MINUTES = attendance.ATTENDANCE_ON_SITE_MINUTES
GATE_NEWER_SECONDS = 120
# How long after leaving through the gate someone stays "outside" / "likely
# outside". Not back after that, they are "not seen recently".
OUTSIDE_MINUTES = float(os.environ.get("ATTENDANCE_OUTSIDE_MINUTES", "40"))


def _location(now: float, sessions: list[dict], current_out: float | None, sightings: list[dict],
              gate_cams: set[int], camera_names: dict[int, str]) -> dict:
    """Where someone is right now, from the newest evidence:
      inside          open gate session, or recognised by an INSIDE camera recently
      outside         a confirmed gate exit, nothing seen inside since
      likely_outside  last recognised at the gate camera, not on any inside camera
                      since (they left without the gate naming them) - verify
      at_gate         only ever seen at the gate camera today (which side unknown)
      unknown         not recognised anywhere recently, or left through the gate
                      more than OUTSIDE_MINUTES ago and not back since"""
    name = lambda c: camera_names.get(c, f"Camera {c}")  # noqa: E731
    clock = lambda t: datetime.datetime.fromtimestamp(t).strftime("%I:%M %p").lstrip("0")  # noqa: E731
    inside_ev = [x for x in sightings if x["camera_id"] not in gate_cams]
    gate_ev = [x for x in sightings if x["camera_id"] in gate_cams]
    last_in = max(inside_ev, key=lambda x: x["last_seen"], default=None)
    last_gate = max(gate_ev, key=lambda x: x["last_seen"], default=None)
    t_in = last_in["last_seen"] if last_in else 0
    t_gate = last_gate["last_seen"] if last_gate else 0
    exit_at = (now - current_out) if current_out else None
    gone = lambda t: now - t > OUTSIDE_MINUTES * 60  # noqa: E731
    gone_detail = f"not back for over {OUTSIDE_MINUTES:g} min"

    if any(x["inside"] for x in sessions):
        entry = max(x["entry"] for x in sessions if x["inside"])
        return {"location": "inside", "location_detail": f"came in through the gate at {clock(entry)}", "location_at": entry}
    if exit_at and t_in <= exit_at:
        if gone(exit_at):
            return {"location": "unknown", "location_detail": f"left through the gate at {clock(exit_at)}, {gone_detail}",
                    "location_at": exit_at}
        return {"location": "outside", "location_detail": f"left through the gate at {clock(exit_at)}", "location_at": exit_at}
    if last_in and now - t_in <= SEEN_INSIDE_MINUTES * 60 and t_in >= t_gate - GATE_NEWER_SECONDS:
        return {"location": "inside", "location_detail": f"seen on {name(last_in['camera_id'])} at {clock(t_in)}", "location_at": t_in}
    if last_gate and last_in and t_gate > t_in + GATE_NEWER_SECONDS:
        if gone(t_gate):
            # Still left without the gate naming them: keep the missing-exit flag.
            return {"location": "unknown", "location_detail": f"last seen at the gate ({name(last_gate['camera_id'])}) at {clock(t_gate)}, {gone_detail}",
                    "location_at": t_gate, "_unconfirmed_exit": True}
        return {"location": "likely_outside",
                "location_detail": f"last seen at the gate ({name(last_gate['camera_id'])}) at {clock(t_gate)}, not inside since {clock(t_in)}",
                "location_at": t_gate}
    if last_gate and not last_in:
        return {"location": "at_gate", "location_detail": f"only seen at the gate ({name(last_gate['camera_id'])}), last at {clock(t_gate)}",
                "location_at": t_gate}
    newest = max(sightings, key=lambda x: x["last_seen"], default=None)
    if newest:
        return {"location": "unknown", "location_detail": f"not seen since {clock(newest['last_seen'])} ({name(newest['camera_id'])})",
                "location_at": newest["last_seen"]}
    return {"location": None, "location_detail": None, "location_at": None}


def _at(day: str, hm: str) -> float:
    return datetime.datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").timestamp()


def classify(employee_id: str, day: str, *, policy: dict, seen: dict | None, sessions: list[dict],
             approved: list[dict], holidays: list[dict], now: float, site: str | None = None,
             sightings: list[dict] | None = None, gate_cams: set[int] | None = None,
             camera_names: dict[int, str] | None = None, corrections: list[dict] | None = None) -> dict:
    """One employee's day: status plus every figure the reports need."""
    is_today = day == datetime.date.fromtimestamp(now).isoformat()
    weekday = DAYS[datetime.date.fromisoformat(day).weekday()]
    holiday = next((h for h in holidays if h["site"] in (None, site)), None)
    leave_full = next((r for r in approved if r["kind"] == "leave" and r["half"] == "full"), None)
    leave_half = next((r for r in approved if r["kind"] == "leave" and r["half"] != "full"), None)
    wfh = next((r for r in approved if r["kind"] == "wfh"), None)

    # In / out: confirmed gate crossings first, face sightings otherwise.
    first_in = min([s["entry"] for s in sessions] + ([seen["first_seen"]] if seen else []), default=None)
    exits = [s["exit"] for s in sessions if s["exit"]]
    last_out = max(exits + ([seen["last_seen"]] if seen else []), default=None) if not any(s["inside"] for s in sessions) else None
    original_first_in, original_last_out = first_in, last_out
    first_in, last_out, corrected = _apply_corrections(first_in, last_out, corrections)
    inside_seconds = sum(((s["exit"] or (now if s["inside"] else s["entry"])) - s["entry"]) for s in sessions)
    outside = []
    ordered = sorted(sessions, key=lambda s: s["entry"])
    for a, b in zip(ordered, ordered[1:]):
        if a["exit"]:
            outside.append(b["entry"] - a["exit"])
    if is_today and ordered and ordered[-1]["exit"] and not ordered[-1]["inside"]:
        current_out = now - ordered[-1]["exit"]   # out right now (may come back)
    else:
        current_out = None
    loc = (_location(now, sessions, current_out, sightings or [], gate_cams or set(), camera_names or {})
           if is_today else {"location": None, "location_detail": None, "location_at": None})
    if is_today and not sightings and not sessions and seen and now - seen["last_seen"] <= SEEN_INSIDE_MINUTES * 60:
        # No per-camera events (older data): seen by face recently.
        loc = {"location": "inside", "location_detail": "seen by face recently", "location_at": seen["last_seen"]}
    unconfirmed_exit = loc.pop("_unconfirmed_exit", False) or loc["location"] == "likely_outside"
    inside_now = loc["location"] == "inside"
    missing_exit = any(s["exit"] and not s["exit_confirmed"] for s in sessions) and "exit" not in corrected
    missing_entry = bool(seen) and not sessions and "entry" not in corrected   # seen by face, never identified at the gate
    long_out_limit = policy["long_outside_minutes"] * 60
    long_outside = any(g > long_out_limit for g in outside) or (current_out or 0) > long_out_limit

    start = _at(day, policy["shift_start"])
    late_cut = start + policy["grace_minutes"] * 60
    half_cut = _at(day, policy["half_day_after"])
    shift_end = _at(day, policy["shift_end"])
    if leave_half and leave_half["half"] == "first":
        late_cut = max(late_cut, half_cut + policy["grace_minutes"] * 60)   # expected only for the second half

    if holiday:
        status = HOLIDAY
    elif weekday in policy["weekly_offs"]:
        status = WEEKLY_OFF
    elif leave_full:
        status = ON_LEAVE
    elif wfh and not first_in:
        status = WFH
    elif first_in:
        if leave_half or first_in > half_cut:
            status = HALF_DAY
        elif first_in > late_cut:
            status = LATE
        else:
            status = PRESENT
    elif is_today and now < late_cut:
        status = NOT_YET
    else:
        status = ABSENT

    worked_day = status in WORKED
    day_over = now >= shift_end
    early = bool(worked_day and day_over and last_out and not leave_half
                 and last_out < shift_end - policy["early_departure_grace_minutes"] * 60)
    overtime_minutes = 0
    if first_in and last_out:
        if status in (HOLIDAY, WEEKLY_OFF):
            overtime_minutes = int((last_out - first_in) // 60)   # any work on a day off
        elif last_out > shift_end + policy["overtime_after_minutes"] * 60:
            overtime_minutes = int((last_out - shift_end) // 60)
    return {
        "employee_id": employee_id, "day": day, "status": status,
        "holiday": holiday["name"] if holiday else None,
        "leave_type": (leave_full or leave_half or {}).get("leave_type"),
        "half_day_leave": leave_half["half"] if leave_half else None,
        "wfh": bool(wfh),
        "first_in": first_in, "last_out": last_out,
        "late": status == LATE, "half_day": status == HALF_DAY,
        "early_departure": early, "overtime_minutes": overtime_minutes,
        "inside_now": inside_now,
        # outside: a confirmed gate exit, or likely outside (verify)
        "outside_now": loc["location"] in ("outside", "likely_outside"),
        "outside_confirmed": loc["location"] == "outside",
        **loc,
        "inside_seconds": int(inside_seconds),
        "outside_seconds": int(sum(outside) + (current_out or 0)),
        "outside_periods": len(outside) + (1 if current_out else 0),
        "long_outside": long_outside,
        "missing_exit": (missing_exit or unconfirmed_exit) and "exit" not in corrected, "missing_entry": missing_entry,
        # Manual corrections in force, and the recorded times they replaced.
        "corrected": bool(corrected), "corrections": list(corrected.values()),
        "presence_verified": "presence_verified" in corrected,
        "original_first_in": original_first_in, "original_last_out": original_last_out,
        "sessions": [{"entry": s["entry"], "exit": s["exit"], "exit_confirmed": s["exit_confirmed"], "inside": s["inside"]} for s in ordered],
        "source": "gate" if sessions else ("face" if seen else None),
        "last_camera": seen.get("last_camera") if seen else None,
        "last_seen": seen.get("last_seen") if seen else None,
    }


def classify_range(day_from: str, day_to: str, employee_ids: list[str] | None = None, now: float | None = None) -> list[dict]:
    """classify() for every employee and every day in the range (inclusive)."""
    now = now if now is not None else time.time()
    policy = get_policy()
    start = datetime.date.fromisoformat(day_from)
    end = datetime.date.fromisoformat(day_to)
    ids = employee_ids or sorted(employee_directory.EMPLOYEES)
    with _conn() as conn:
        seen = {(r["employee_id"], r["day"]): dict(r) for r in conn.execute(
            "SELECT * FROM attendance_days WHERE day >= ? AND day <= ?", (day_from, day_to))}
    start_ts = datetime.datetime.combine(start, datetime.time()).timestamp()
    end_ts = datetime.datetime.combine(end + datetime.timedelta(days=1), datetime.time()).timestamp()
    sessions = _sessions_between(start_ts, end_ts)
    sightings = _sightings_between(start_ts, end_ts)
    gate_cams = _gate_cameras()
    from app import camera_db

    camera_names = {c["id"]: c["name"] for c in camera_db.list_cameras()}
    approved = _approved_between(day_from, day_to)
    holidays = _holidays_between(day_from, day_to)
    corrections = _active_corrections(day_from, day_to)
    today = datetime.date.fromtimestamp(now)
    out = []
    d = start
    while d <= end and d <= today:
        iso = d.isoformat()
        for emp in ids:
            out.append(classify(emp, iso, policy=policy, seen=seen.get((emp, iso)), sessions=sessions.get((emp, iso), []),
                                approved=approved.get((emp, iso), []), holidays=holidays.get(iso, []), now=now,
                                sightings=sightings.get((emp, iso), []), gate_cams=gate_cams, camera_names=camera_names,
                                corrections=corrections.get((emp, iso), [])))
        d += datetime.timedelta(days=1)
    return out


def month_counts(day: str, now: float | None = None) -> dict[str, dict]:
    """Per employee, from the 1st of `day`'s month to `day`: lates, half days,
    absences, WFH and leave days, early departures, overtime minutes."""
    first = day[:8] + "01"
    counts: dict[str, dict] = {}
    for r in classify_range(first, day, now=now):
        c = counts.setdefault(r["employee_id"], {"late": 0, "half_day": 0, "absent": 0, "wfh": 0, "leave": 0,
                                                  "present": 0, "early": 0, "overtime_minutes": 0, "working_days": 0})
        if r["status"] not in (HOLIDAY, WEEKLY_OFF, NOT_YET):
            c["working_days"] += 1
        c["late"] += r["late"]
        c["half_day"] += r["half_day"]
        c["absent"] += r["status"] == ABSENT
        c["wfh"] += r["status"] == WFH
        c["leave"] += r["status"] == ON_LEAVE
        c["present"] += r["status"] in WORKED
        c["early"] += r["early_departure"]
        c["overtime_minutes"] += r["overtime_minutes"]
    return counts


# --------------------------------------------------------------------------- the Attendance dashboard

# KPI cards: key -> (label, which rows it lists). Every card is a filter.
KPI_FILTERS = {
    "total": ("Total employees", lambda r, m, p: True),
    "inside": ("Present inside", lambda r, m, p: r["inside_now"]),
    "outside": ("Currently outside", lambda r, m, p: r["outside_now"]),
    "wfh": ("Working from home", lambda r, m, p: r["status"] == WFH),
    "leave": ("On leave", lambda r, m, p: r["status"] == ON_LEAVE or bool(r["half_day_leave"])),
    "late": ("Late arrivals", lambda r, m, p: r["late"]),
    "half_day": ("Half-day", lambda r, m, p: r["half_day"]),
    "absent": ("Absent", lambda r, m, p: r["status"] == ABSENT),
    "overtime": ("Overtime", lambda r, m, p: r["overtime_minutes"] > 0),
    "late_limit": ("Over late-arrival limit", lambda r, m, p: m["late"] > p["late_allowance_per_month"]),
    "half_day_limit": ("Over half-day limit", lambda r, m, p: m["half_day"] > p["half_day_allowance_per_month"]),
    "missing": ("Missing entry/exit", lambda r, m, p: r["missing_entry"] or r["missing_exit"]),
    "long_outside": ("Long time outside", lambda r, m, p: r["long_outside"]),
}


def overview(day: str, department: str | None = None, kpi: str | None = None, now: float | None = None) -> dict:
    """Everything the Attendance page needs for one day: KPI cards (each with
    the count of employees it lists), the employee rows (optionally only one
    card's), pending requests and upcoming holidays."""
    now = now if now is not None else time.time()
    policy = get_policy()
    emps = {e: v for e, v in employee_directory.EMPLOYEES.items() if not department or (v.get("company") or "") == department}
    rows = [r for r in classify_range(day, day, employee_ids=sorted(emps), now=now)] if emps else []
    month = month_counts(day, now=now)
    blank = {"late": 0, "half_day": 0, "absent": 0, "wfh": 0, "leave": 0, "present": 0, "early": 0, "overtime_minutes": 0, "working_days": 0}
    cards = []
    for key, (label, test) in KPI_FILTERS.items():
        cards.append({"key": key, "label": label, "count": sum(1 for r in rows if test(r, month.get(r["employee_id"], blank), policy))})
    pending = list_requests(status="pending")
    pending = [r for r in pending if r["employee_id"] in emps]
    cards.append({"key": "pending", "label": "Pending leave/WFH requests", "count": len(pending)})
    holidays = upcoming_holidays(datetime.date.fromtimestamp(now).isoformat())
    cards.append({"key": "holidays", "label": "Upcoming holidays", "count": len(holidays),
                  "next": holidays[0] if holidays else None})

    listed = rows
    if kpi in KPI_FILTERS:
        test = KPI_FILTERS[kpi][1]
        listed = [r for r in rows if test(r, month.get(r["employee_id"], blank), policy)]
    out_rows = []
    for r in listed:
        e = emps.get(r["employee_id"], {})
        m = month.get(r["employee_id"], blank)
        out_rows.append({**r, "name": e.get("name") or r["employee_id"], "department": e.get("company") or "",
                         "month": m,
                         "over_late_limit": m["late"] > policy["late_allowance_per_month"],
                         "over_half_day_limit": m["half_day"] > policy["half_day_allowance_per_month"]})
    return {
        "date": day, "department": department, "kpi": kpi, "policy": policy,
        "departments": sorted({(v.get("company") or "") for v in employee_directory.EMPLOYEES.values()} - {""}),
        "cards": cards, "rows": out_rows, "pending_requests": pending, "upcoming_holidays": holidays,
    }
