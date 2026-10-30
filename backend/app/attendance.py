"""
attendance.py

Marks employee attendance from face recognition. face_pipeline.py calls
record() whenever it identifies an employee on a camera that has face
recognition on (the camera's attendance_tracking flag); this keeps each
employee's first and last sighting per day, which is all the Attendance
page needs: time in, time out, time stayed, on time vs late, and whether
they're on site right now. Leave marked by HR is stored here too, so an
employee on approved leave shows "On Leave" instead of "Absent".

Only confident identifications count (ATTENDANCE_MIN_CONFIDENCE): a wrong
check-in is worse than a missed one, since the next sighting will usually
catch a missed one anyway.
"""

import datetime
import os
import sqlite3
import threading
import time

from app import camera_db, employee_directory
from app import config, db

DB_PATH = config.DB_PATH

# Classifier probability needed for a sighting to count. Measured on
# held-out data, reads at >= 0.8 were wrong ~0.3% of the time.
ATTENDANCE_MIN_CONFIDENCE = float(os.environ.get("ATTENDANCE_MIN_CONFIDENCE", "0.8"))
# Arriving after this local time (HH:MM) is a late arrival.
ATTENDANCE_LATE_AFTER = os.environ.get("ATTENDANCE_LATE_AFTER", "09:30")
# Seen within this many minutes = "On site"; seen earlier today = "Present".
ATTENDANCE_ON_SITE_MINUTES = float(os.environ.get("ATTENDANCE_ON_SITE_MINUTES", "15"))
# The same employee is re-recognised several times a second while in view;
# write at most one update per this many seconds (the first of the day is
# always written immediately).
WRITE_EVERY_SECONDS = 20
# End of the working day (HH:MM): last seen more than ATTENDANCE_EARLY_GRACE_MINUTES
# before it = early departure; more than ATTENDANCE_OVERTIME_MINUTES after = overtime.
ATTENDANCE_SHIFT_END = os.environ.get("ATTENDANCE_SHIFT_END", "18:00")
ATTENDANCE_EARLY_GRACE_MINUTES = float(os.environ.get("ATTENDANCE_EARLY_GRACE_MINUTES", "15"))
ATTENDANCE_OVERTIME_MINUTES = float(os.environ.get("ATTENDANCE_OVERTIME_MINUTES", "30"))
# KPI: employees late this many times or more in the last 30 days.
LATE_REPEAT_COUNT = int(os.environ.get("ATTENDANCE_LATE_REPEAT_COUNT", "3"))
# A person seen again on the same camera within this gap is the same
# recognition event (face_recognition_events).
RECOGNITION_EVENT_GAP_SECONDS = float(os.environ.get("RECOGNITION_EVENT_GAP_SECONDS", "600"))

_last_write: dict[str, float] = {}
# "employee|camera" -> [event row id, last_seen, last time the row was written]
_open_events: dict[str, list] = {}
_lock = threading.Lock()


def _conn():
    """Shared connection policy (app/db.py): lock timeout + busy_timeout,
    commit on success, rollback on error, and always closed."""
    return db.connect(DB_PATH, row_factory=sqlite3.Row)


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance_days (
                employee_id TEXT NOT NULL,
                day TEXT NOT NULL,            -- local date, YYYY-MM-DD
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                first_camera INTEGER,
                last_camera INTEGER,
                sightings INTEGER NOT NULL DEFAULT 1,
                best_confidence REAL,
                PRIMARY KEY (employee_id, day)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance_leave (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                day_from TEXT NOT NULL,
                day_to TEXT NOT NULL,
                reason TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        # Face Recognition report history: one row per RECOGNITION EVENT, i.e.
        # one person seen on one camera; sightings closer together than
        # RECOGNITION_EVENT_GAP_SECONDS extend the same row, so a person standing
        # in front of a camera is one event, not one per frame. No images.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS face_recognition_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id TEXT NOT NULL,
                camera_id INTEGER,
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                sightings INTEGER NOT NULL DEFAULT 1,
                best_confidence REAL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_face_recognition_events_ts ON face_recognition_events (first_seen)")


def _day(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def record(employee_id: str, camera_id: int, confidence: float, ts: float | None = None) -> bool:
    """One recognised sighting. Returns True if it was written."""
    if not employee_id or confidence < ATTENDANCE_MIN_CONFIDENCE:
        return False
    if employee_directory.get_employee(employee_id) is None:
        return False  # not a known employee — nothing to mark
    ts = ts if ts is not None else time.time()
    try:
        _log_recognition(employee_id, camera_id, confidence, ts)
    except sqlite3.Error:
        pass  # report history must never stop attendance
    day = _day(ts)
    key = f"{employee_id}|{day}"
    with _lock:
        if ts - _last_write.get(key, 0) < WRITE_EVERY_SECONDS:
            return False
        _last_write[key] = ts
    with _conn() as conn:
        first_today = conn.execute(
            "SELECT 1 FROM attendance_days WHERE employee_id = ? AND day = ?", (employee_id, day)).fetchone() is None
        conn.execute(
            """
            INSERT INTO attendance_days (employee_id, day, first_seen, last_seen, first_camera, last_camera, sightings, best_confidence)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(employee_id, day) DO UPDATE SET
                first_seen = MIN(first_seen, excluded.first_seen),
                last_seen = MAX(last_seen, excluded.last_seen),
                last_camera = excluded.last_camera,
                sightings = sightings + 1,
                best_confidence = MAX(COALESCE(best_confidence, 0), excluded.best_confidence)
            """,
            (employee_id, day, ts, ts, camera_id, camera_id, confidence),
        )
    if first_today and ts > _late_cutoff(day):
        from app import alerts

        alerts.late_arrival(employee_id, employee_directory.get_employee(employee_id)["name"], camera_id, ts)
    return True


def _log_recognition(employee_id: str, camera_id: int, confidence: float, ts: float) -> None:
    """Extends this person's open recognition event on this camera, or starts
    a new one after a gap. The row is rewritten at most every
    WRITE_EVERY_SECONDS, so the recognition loop is not slowed by it."""
    key = f"{employee_id}|{camera_id}"
    with _lock:
        ev = _open_events.get(key)
        if ev is not None and ts - ev[1] <= RECOGNITION_EVENT_GAP_SECONDS:
            ev[1] = ts
            if ts - ev[2] < WRITE_EVERY_SECONDS:
                return
            ev[2] = ts
            row_id = ev[0]
        else:
            row_id = None
    with _conn() as conn:
        if row_id is not None:
            conn.execute(
                "UPDATE face_recognition_events SET last_seen = MAX(last_seen, ?), sightings = sightings + 1, "
                "best_confidence = MAX(COALESCE(best_confidence, 0), ?) WHERE id = ?",
                (ts, confidence, row_id),
            )
            return
        cur = conn.execute(
            "INSERT INTO face_recognition_events (employee_id, camera_id, first_seen, last_seen, sightings, best_confidence) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (employee_id, camera_id, ts, ts, confidence),
        )
    with _lock:
        _open_events[key] = [cur.lastrowid, ts, ts]


def add_leave(employee_id: str, day_from: str, day_to: str, reason: str) -> dict:
    if employee_directory.get_employee(employee_id) is None:
        raise ValueError(f"Unknown employee {employee_id}")
    if day_to < day_from:
        raise ValueError("Leave must end on or after the day it starts")
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO attendance_leave (employee_id, day_from, day_to, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (employee_id, day_from, day_to, reason, time.time()),
        )
    return {"id": cur.lastrowid, "employee_id": employee_id, "day_from": day_from, "day_to": day_to}


def _clock(ts: float | None) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%I:%M %p") if ts else "-"


def _stay(first: float | None, last: float | None) -> str:
    if not first or not last:
        return "-"
    mins = int((last - first) // 60)
    return f"{mins // 60}h {mins % 60:02d}m"


def _shift_end(day: str) -> float:
    h, m = (int(x) for x in ATTENDANCE_SHIFT_END.split(":"))
    return datetime.datetime.strptime(day, "%Y-%m-%d").replace(hour=h, minute=m).timestamp()


def _departure(s: dict | None, day: str, now: float) -> tuple[str, int]:
    """("Early departure" | "Overtime" | "Normal" | "-", overtime minutes).
    Early departure is only decided once the shift has ended (before that,
    someone not seen recently may simply be elsewhere in the building)."""
    if not s:
        return "-", 0
    end = _shift_end(day)
    over = (s["last_seen"] - end) / 60
    if over > ATTENDANCE_OVERTIME_MINUTES:
        return "Overtime", int(over)
    if now >= end and s["last_seen"] < end - ATTENDANCE_EARLY_GRACE_MINUTES * 60:
        return "Early departure", 0
    return ("Normal" if now >= end else "-"), 0


def _late_cutoff(day: str) -> float:
    h, m = (int(x) for x in ATTENDANCE_LATE_AFTER.split(":"))
    d = datetime.datetime.strptime(day, "%Y-%m-%d")
    return d.replace(hour=h, minute=m).timestamp()


def _status(seen: dict | None, on_leave: bool, day: str, now: float) -> str:
    if seen:
        if day == _day(now) and now - seen["last_seen"] <= ATTENDANCE_ON_SITE_MINUTES * 60:
            return "On site"
        return "Present"
    return "On Leave" if on_leave else "Absent"


def day_report(day: str, now: float | None = None) -> dict:
    """Every employee's attendance for one local date, plus the headline
    numbers for the Attendance page's stat cards."""
    now = now if now is not None else time.time()
    with _conn() as conn:
        seen = {r["employee_id"]: dict(r) for r in conn.execute("SELECT * FROM attendance_days WHERE day = ?", (day,))}
        on_leave = {r[0] for r in conn.execute(
            "SELECT employee_id FROM attendance_leave WHERE day_from <= ? AND day_to >= ?", (day, day))}
    cameras = {c["id"]: c["name"] for c in camera_db.list_cameras()}
    late_cutoff = _late_cutoff(day)

    rows = []
    for emp_id, emp in sorted(employee_directory.EMPLOYEES.items()):
        s = seen.get(emp_id)
        status = _status(s, emp_id in on_leave, day, now)
        departure, overtime = _departure(s, day, now)
        rows.append({
            "departure": departure,
            "overtime_minutes": overtime,
            "employee_id": emp_id,
            "name": emp["name"],
            "company": emp["company"] or "No company",
            "status": status,
            "time_in": _clock(s["first_seen"]) if s else "-",
            # Time out is always their last sighting so far (it moves forward
            # while they're still around), and time stay runs first -> last.
            "time_out": _clock(s["last_seen"]) if s else "-",
            "time_stay": _stay(s["first_seen"], s["last_seen"]) if s else "-",
            "arrival": ("Late arrival" if s["first_seen"] > late_cutoff else "On time") if s else "-",
            "camera": cameras.get(s["last_camera"], f"Camera {s['last_camera']}") if s else "-",
            "last_seen": _clock(s["last_seen"]) if s else "-",
            "confidence": round((s["best_confidence"] or 0) * 100) if s else 0,
            "sightings": s["sightings"] if s else 0,
        })

    total = len(rows)
    present = sum(r["status"] in ("Present", "On site") for r in rows)

    # KPI cards: employees late LATE_REPEAT_COUNT+ times in the 30 days up to
    # this day, and employees present with a single sighting (no time-out yet).
    window_start = (datetime.datetime.strptime(day, "%Y-%m-%d") - datetime.timedelta(days=29)).strftime("%Y-%m-%d")
    with _conn() as conn:
        late_days: dict[str, int] = {}
        for r in conn.execute("SELECT employee_id, day, first_seen FROM attendance_days WHERE day BETWEEN ? AND ?", (window_start, day)):
            if r["employee_id"] in employee_directory.EMPLOYEES and r["first_seen"] > _late_cutoff(r["day"]):
                late_days[r["employee_id"]] = late_days.get(r["employee_id"], 0) + 1
    single = sum(1 for emp_id, s in seen.items()
                 if emp_id in employee_directory.EMPLOYEES and s["last_seen"] - s["first_seen"] < 60)
    return {
        "date": day,
        "late_after": ATTENDANCE_LATE_AFTER,
        "rows": rows,
        "stats": {
            "present": present,
            "total": total,
            "absent": sum(r["status"] == "Absent" for r in rows),
            "on_leave": sum(r["status"] == "On Leave" for r in rows),
            "late": sum(r["arrival"] == "Late arrival" for r in rows),
            "attendance_pct": round(present / total * 100, 1) if total else 0,
            "late_repeat": sum(1 for n in late_days.values() if n >= LATE_REPEAT_COUNT),
            "late_repeat_threshold": LATE_REPEAT_COUNT,
            "seen_once": single,
            "early_departures": sum(r["departure"] == "Early departure" for r in rows),
            "overtime": sum(r["departure"] == "Overtime" for r in rows),
            "overtime_minutes": sum(r["overtime_minutes"] for r in rows),
            "shift_end": ATTENDANCE_SHIFT_END,
        },
        # Employees late LATE_REPEAT_COUNT+ times in 30 days (names for the exceptions list).
        "late_repeat_ids": sorted(e for e, n in late_days.items() if n >= LATE_REPEAT_COUNT),
    }


def history(employee_id: str, days: int = 14, now: float | None = None) -> list[dict]:
    """Last `days` days for one employee, newest first."""
    now = now if now is not None else time.time()
    today = datetime.date.fromtimestamp(now)
    out = []
    with _conn() as conn:
        for i in range(days):
            d = (today - datetime.timedelta(days=i)).isoformat()
            s = conn.execute("SELECT * FROM attendance_days WHERE employee_id = ? AND day = ?", (employee_id, d)).fetchone()
            leave = conn.execute(
                "SELECT 1 FROM attendance_leave WHERE employee_id = ? AND day_from <= ? AND day_to >= ?", (employee_id, d, d)
            ).fetchone()
            if s:
                status = "Late arrival" if s["first_seen"] > _late_cutoff(d) else "Present"
            else:
                status = "On Leave" if leave else "Absent"
            out.append({"date": d, "status": status, "time_in": _clock(s["first_seen"]) if s else "-",
                        "time_out": _clock(s["last_seen"]) if s else "-"})
    return out
