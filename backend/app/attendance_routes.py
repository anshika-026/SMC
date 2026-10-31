"""
attendance_routes.py

Attendance marked from face recognition (see attendance.py).

Mount with: app.include_router(attendance_routes.router) in main.py
"""

import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import attendance, auth, presence, workforce

router = APIRouter(prefix="/api/attendance", tags=["attendance"])


def _valid_day(day: str) -> str:
    try:
        datetime.date.fromisoformat(day)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must look like 2026-09-28")
    return day


@router.get("")
def attendance_for_day(date: str | None = None, _: dict = Depends(auth.require_admin)):
    """Every employee's attendance for `date` (YYYY-MM-DD, default today)."""
    return attendance.day_report(_valid_day(date or datetime.date.today().isoformat()))


@router.get("/{employee_id}/history")
def attendance_history(employee_id: str, days: int = 14, _: dict = Depends(auth.require_admin)):
    return attendance.history(employee_id, days=max(1, min(days, 90)))


class LeaveIn(BaseModel):
    employee_id: str
    day_from: str
    day_to: str
    reason: str = ""


@router.post("/leave")
def mark_leave(payload: LeaveIn, _: dict = Depends(auth.require_admin)):
    try:
        return attendance.add_leave(payload.employee_id, _valid_day(payload.day_from), _valid_day(payload.day_to), payload.reason)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ---------------------------------------------------------------------------
# Workforce management (workforce.py): policy, holidays, leave & WFH requests,
# balances, and the day overview with KPI cards. HR/admin only.
# ---------------------------------------------------------------------------

from app import workforce  # noqa: E402


def _bad(e: Exception):
    raise HTTPException(status_code=422, detail=str(e))


# ---- Presence investigation and manual corrections (presence.py, workforce.py)

@router.get("/main-hall")
def main_hall(_: dict = Depends(auth.require_admin)):
    """The two cameras covering the main hall (Camera 01 left, Camera 02 right)."""
    return presence.get_main_hall()


class MainHallIn(BaseModel):
    camera_ids: list[int]


@router.put("/main-hall")
def set_main_hall(body: MainHallIn, principal: dict = Depends(auth.require_admin)):
    try:
        return presence.set_main_hall(body.camera_ids, principal)
    except ValueError as e:
        _bad(e)


@router.get("/cameras/locations")
def camera_locations(_: dict = Depends(auth.require_admin)):
    """Every camera with its mapped location (from configuration) and stream availability."""
    return {"cameras": presence.camera_directory()}


class ZoneIn(BaseModel):
    zone: str | None = None


@router.put("/cameras/{camera_id}/location")
def set_camera_location(camera_id: int, body: ZoneIn, principal: dict = Depends(auth.require_admin)):
    try:
        return {"cameras": presence.set_camera_zone(camera_id, body.zone, principal)}
    except ValueError as e:
        _bad(e)


@router.get("/employees/{employee_id}/presence")
def employee_presence(employee_id: str, date: str | None = None, _: dict = Depends(auth.require_admin)):
    """Where the employee has been seen (live and today), and whether that is verified."""
    try:
        return presence.investigate(employee_id, _valid_day(date) if date else None)
    except KeyError:
        raise HTTPException(status_code=404, detail="Employee not found")


@router.get("/employees/{employee_id}/corrections")
def employee_corrections(employee_id: str, date: str | None = None, _: dict = Depends(auth.require_admin)):
    return {"corrections": workforce.list_corrections(employee_id, _valid_day(date) if date else None)}


class CorrectionIn(BaseModel):
    employee_id: str
    day: str
    kind: str
    ts: float
    reason: str
    evidence: str | None = None


@router.post("/corrections")
def add_correction(body: CorrectionIn, principal: dict = Depends(auth.require_admin)):
    try:
        return workforce.add_correction(body.employee_id, _valid_day(body.day), body.kind, body.ts, body.reason, body.evidence, principal)
    except ValueError as e:
        _bad(e)


class VoidIn(BaseModel):
    reason: str


@router.post("/corrections/{correction_id}/withdraw")
def withdraw_correction(correction_id: int, body: VoidIn, principal: dict = Depends(auth.require_admin)):
    try:
        return workforce.void_correction(correction_id, body.reason, principal)
    except KeyError:
        raise HTTPException(status_code=404, detail="Correction not found")
    except ValueError as e:
        _bad(e)


@router.get("/overview")
def attendance_overview(date: str | None = None, department: str | None = None, kpi: str | None = None,
                        _: dict = Depends(auth.require_admin)):
    """KPI cards + every employee's classified day; `kpi` lists only one card's employees."""
    return workforce.overview(_valid_day(date or datetime.date.today().isoformat()), department or None, kpi or None)


@router.get("/range")
def attendance_range(date_from: str, date_to: str, employee_id: str | None = None, _: dict = Depends(auth.require_admin)):
    """Classified days for a range (max 92 days), e.g. one employee's month."""
    a, b = _valid_day(date_from), _valid_day(date_to)
    if b < a or (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days > 92:
        raise HTTPException(status_code=422, detail="Pick a range of up to 92 days")
    return {"rows": workforce.classify_range(a, b, employee_ids=[employee_id] if employee_id else None)}


@router.get("/policy")
def get_policy(_: dict = Depends(auth.require_admin)):
    return {"policy": workforce.get_policy(), "defaults": workforce.default_policy(), "weekdays": workforce.DAYS}


@router.put("/policy")
def update_policy(changes: dict, principal: dict = Depends(auth.require_admin)):
    try:
        return {"policy": workforce.set_policy(changes, principal)}
    except ValueError as e:
        _bad(e)


class HolidayIn(BaseModel):
    day: str
    name: str
    site: str | None = None


@router.get("/holidays")
def holidays(year: int | None = None, _: dict = Depends(auth.require_admin)):
    return {"holidays": workforce.list_holidays(year)}


@router.post("/holidays")
def add_holiday(body: HolidayIn, principal: dict = Depends(auth.require_admin)):
    try:
        return workforce.save_holiday(_valid_day(body.day), body.name, body.site, principal)
    except ValueError as e:
        _bad(e)


@router.put("/holidays/{holiday_id}")
def edit_holiday(holiday_id: int, body: HolidayIn, principal: dict = Depends(auth.require_admin)):
    try:
        return workforce.save_holiday(_valid_day(body.day), body.name, body.site, principal, holiday_id=holiday_id)
    except ValueError as e:
        _bad(e)


@router.delete("/holidays/{holiday_id}")
def remove_holiday(holiday_id: int, principal: dict = Depends(auth.require_admin)):
    if not workforce.delete_holiday(holiday_id, principal):
        raise HTTPException(status_code=404, detail="No such holiday")
    return {"ok": True}


class LeaveTypeIn(BaseModel):
    code: str
    name: str
    annual_days: float
    carry_forward_max: float = 0
    expires_months: int = 0
    active: bool = True


@router.get("/leave-types")
def get_leave_types(_: dict = Depends(auth.require_admin)):
    return {"leave_types": workforce.leave_types()}


@router.put("/leave-types")
def put_leave_type(body: LeaveTypeIn, principal: dict = Depends(auth.require_admin)):
    try:
        return workforce.save_leave_type(body.code, body.name, body.annual_days, body.carry_forward_max,
                                         body.expires_months, body.active, principal)
    except ValueError as e:
        _bad(e)


@router.get("/balances")
def get_balances(year: int | None = None, employee_id: str | None = None, _: dict = Depends(auth.require_admin)):
    from app import employee_directory

    year = year or datetime.date.today().year
    ids = [employee_id] if employee_id else sorted(employee_directory.EMPLOYEES)
    return {"year": year, "employees": [
        {"employee_id": e, "name": (employee_directory.get_employee(e) or {}).get("name") or e,
         "department": (employee_directory.get_employee(e) or {}).get("company") or "",
         "balances": workforce.balance(e, year)} for e in ids]}


class AllocationIn(BaseModel):
    employee_id: str
    leave_type: str
    year: int
    allotted: float | None = None
    carried_forward: float | None = None
    adjustment: float | None = None
    note: str | None = None


@router.put("/balances")
def put_allocation(body: AllocationIn, principal: dict = Depends(auth.require_admin)):
    try:
        return workforce.set_allocation(body.employee_id, body.leave_type, body.year, body.allotted,
                                        body.carried_forward, body.adjustment, body.note, principal)
    except ValueError as e:
        _bad(e)


@router.post("/balances/allocate-defaults")
def allocate_defaults(year: int, principal: dict = Depends(auth.require_admin)):
    """Gives everyone without an allocation for `year` their leave types'
    annual days (+ carry-forward). Never changes an existing allocation."""
    return {"created": workforce.allocate_defaults(year, principal)}


class RequestIn(BaseModel):
    employee_id: str
    kind: str                 # leave | wfh
    day_from: str
    day_to: str
    half: str = "full"        # full | first | second
    leave_type: str | None = None
    reason: str = ""
    approve: bool = False     # record as already approved


@router.get("/requests")
def get_requests(status: str | None = None, employee_id: str | None = None, kind: str | None = None,
                 date_from: str | None = None, date_to: str | None = None, _: dict = Depends(auth.require_admin)):
    return {"requests": workforce.list_requests(status, employee_id, kind, date_from, date_to)}


@router.post("/requests")
def add_request(body: RequestIn, principal: dict = Depends(auth.require_admin)):
    try:
        return workforce.create_request(body.employee_id, body.kind, _valid_day(body.day_from), _valid_day(body.day_to),
                                        body.half, body.leave_type, body.reason, principal, body.approve)
    except ValueError as e:
        _bad(e)


class DecisionIn(BaseModel):
    decision: str             # approved | rejected | cancelled
    note: str = ""


@router.post("/requests/{request_id}/decision")
def decide(request_id: int, body: DecisionIn, principal: dict = Depends(auth.require_admin)):
    try:
        return workforce.decide_request(request_id, body.decision, body.note, principal)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        _bad(e)
