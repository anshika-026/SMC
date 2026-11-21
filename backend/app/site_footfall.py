"""Unique footfall: the people who came to the site each day, each counted once.

Counting line crossings at one gate camera missed most people here (a few a day
while 10-13 employees were recognised daily), so footfall is the people the
cameras actually saw that day:
  employees  recognised by face on any camera (attendance.py's
             face_recognition_events), once per day, however many cameras saw them
  visitors   visitors seen in a visitor area (visitors.py), one per visitor
             number (#1, #2, ...) per day, however many visits they made
A person's arrival is their first sighting that day (arrivals per hour, peak
hour); they have left once nobody has seen them for LEFT_AFTER_MINUTES (any
earlier day: everyone has left).
"""

from __future__ import annotations

import datetime
import os
import time

from app import attendance, employee_directory, visitors

LEFT_AFTER_MINUTES = float(os.environ.get("FOOTFALL_LEFT_AFTER_MINUTES", "30"))


def _bounds(day_from: str, day_to: str) -> tuple[float, float]:
    a = datetime.date.fromisoformat(day_from)
    b = datetime.date.fromisoformat(day_to)
    return (datetime.datetime.combine(a, datetime.time()).timestamp(),
            datetime.datetime.combine(b + datetime.timedelta(days=1), datetime.time()).timestamp())


def _day(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def people(day_from: str, day_to: str | None = None) -> list[dict]:
    """Every person seen on each day of [day_from, day_to], once per day:
    {day, kind: employee | visitor, key, name, employee_id, visitor_number,
     first_seen, last_seen, cameras}. Oldest arrival first."""
    day_to = day_to or day_from
    t0, t1 = _bounds(day_from, day_to)
    out: dict[tuple[str, str], dict] = {}
    try:
        with attendance._conn() as conn:
            rows = conn.execute(
                "SELECT employee_id, camera_id, first_seen, last_seen FROM face_recognition_events"
                " WHERE employee_id IS NOT NULL AND employee_id <> '' AND last_seen >= ? AND first_seen < ?", (t0, t1)).fetchall()
    except Exception:  # noqa: BLE001 - face recognition never ran here
        rows = []
    # Employees count once face recognition saw them often enough that day (attendance.MIN_SIGHTINGS).
    confirmed_days = set()
    with attendance._conn() as conn:
        for r in conn.execute("SELECT employee_id, day, sightings FROM attendance_days WHERE day >= ? AND day <= ?", (day_from, day_to)):
            if attendance.confirmed(r):
                confirmed_days.add((r[0], r[1]))
    for emp, cam, first, last in rows:
        first = max(first, t0)
        if (emp, _day(first)) not in confirmed_days:
            continue
        key = (_day(first), f"e{emp}")
        p = out.get(key)
        if p is None:
            out[key] = {"day": key[0], "kind": "employee", "key": key[1], "employee_id": emp, "visitor_number": None,
                        "first_seen": first, "last_seen": last, "cameras": {cam}}
        else:
            p["first_seen"] = min(p["first_seen"], first)
            p["last_seen"] = max(p["last_seen"], last)
            p["cameras"].add(cam)
    visitors.assign_visitor_numbers()
    vis = [v for v in visitors.mark_employee_sittings(visitors.list_visits(limit=10_000_000, since=t0, until=t1))
           if v["kind"] == "visitor"]
    numbers = visitors.visitor_numbers([v["visit"] for v in vis])
    for v in vis:
        n = (numbers.get(v["visit"]) or {}).get("number")
        key = (_day(v["arrived_at"]), f"v{n}" if n else f"x{v['visit']}")
        last = v.get("last_seen") or v["arrived_at"]
        p = out.get(key)
        if p is None:
            out[key] = {"day": key[0], "kind": "visitor", "key": key[1], "employee_id": None, "visitor_number": n,
                        "first_seen": v["arrived_at"], "last_seen": last, "cameras": {v["camera_id"]}}
        else:
            p["first_seen"] = min(p["first_seen"], v["arrived_at"])
            p["last_seen"] = max(p["last_seen"], last)
            p["cameras"].add(v["camera_id"])
    names: dict[str, str] = {}
    now = time.time()
    result = []
    for p in sorted(out.values(), key=lambda p: p["first_seen"]):
        if p["kind"] == "employee":
            if p["employee_id"] not in names:
                names[p["employee_id"]] = (employee_directory.get_employee(p["employee_id"]) or {}).get("name") or p["employee_id"]
            p["name"] = names[p["employee_id"]]
        else:
            p["name"] = f"Visitor #{p['visitor_number']}" if p["visitor_number"] else "Visitor"
        p["cameras"] = sorted(p["cameras"])
        p["left"] = p["day"] != _day(now) or now - p["last_seen"] >= LEFT_AFTER_MINUTES * 60
        result.append(p)
    return result


def day_stats(day: str) -> dict:
    """One day: unique people (entries), employees / visitors, people who have left (exits),
    arrivals per hour (first sighting), the peak hour and the previous day's count."""
    prev = (datetime.date.fromisoformat(day) - datetime.timedelta(days=1)).isoformat()
    ppl = people(prev, day)
    today = [p for p in ppl if p["day"] == day]
    hourly: dict[int, int] = {}
    for p in today:
        h = datetime.datetime.fromtimestamp(p["first_seen"]).hour
        hourly[h] = hourly.get(h, 0) + 1
    employees = sum(1 for p in today if p["kind"] == "employee")
    return {
        "entries": len(today),
        "previous_day_entries": sum(1 for p in ppl if p["day"] == prev),
        "exits": sum(1 for p in today if p["left"]),
        "employee_entries": employees,
        "visitor_entries": len(today) - employees,
        "hourly": [{"hour": h, "entries": hourly[h]} for h in sorted(hourly)],
        "peak_hour": max(hourly, key=hourly.get) if hourly else None,
        "source": "people seen",
    }


def daily(day_from: str, day_to: str) -> list[dict]:
    """Per day in [day_from, day_to] (every day, 0 when nobody came): entries (unique
    people), exits (people who had left), employee entries."""
    by_day: dict[str, list[dict]] = {}
    for p in people(day_from, day_to):
        by_day.setdefault(p["day"], []).append(p)
    out, d, end = [], datetime.date.fromisoformat(day_from), datetime.date.fromisoformat(day_to)
    while d <= end:
        ps = by_day.get(d.isoformat(), [])
        out.append({"day": d.isoformat(), "entries": len(ps), "exits": sum(1 for p in ps if p["left"]),
                    "employee_entries": sum(1 for p in ps if p["kind"] == "employee")})
        d += datetime.timedelta(days=1)
    return out
