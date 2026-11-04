"""
visitor_routes.py

Visitor Management API (admin only; see visitors.py for the rules).

  GET    /api/visitors/cameras                  every camera: online?, saved area, detection active?
  GET    /api/visitors/cameras/{id}/frame       a still from the camera (to check an area against)
  PUT    /api/visitors/zones/{id}               save the area {polygon: [[x, y], ...], enabled, height}
                                                 (polygon = floor; height 0..1 of the frame, 0 = flat; omitted = 0)
  DELETE /api/visitors/zones/{id}               clear the area (stops detection on that camera)
  GET    /api/visitors/events                   newest first; ?camera_id= &after_id= &limit=
  GET    /api/visitors/events/{id}              one event
  GET    /api/visitors/events/{id}/clip         the 10-second MP4 (supports Range / seeking)
  DELETE /api/visitors/events/{id}              remove an event and its clip now
  GET    /api/visitors/settings                 the rules in force (for the UI to show)
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import audit, auth, camera_db, camera_stream, employee_directory, storage, visitors

router = APIRouter(prefix="/api/visitors", tags=["visitors"], dependencies=[Depends(auth.require_admin)])


def _camera(camera_id: int) -> dict:
    cam = camera_db.get_camera(camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    return cam


@router.get("/cameras")
def list_cameras():
    out = []
    for cam in camera_db.list_cameras():
        if not cam.get("is_configured"):
            continue
        out.append({"camera_id": cam["id"], "name": cam["name"], "site": cam.get("site"), **visitors.service.camera_status(cam["id"])})
    return {"cameras": out}


@router.get("/cameras/{camera_id}/frame")
def camera_frame(camera_id: int):
    _camera(camera_id)
    jpeg = camera_stream.grab_still(camera_id)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="No picture from this camera right now - is it online?")
    return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


class ZoneIn(BaseModel):
    polygon: list[list[float]]
    enabled: bool = True
    height: float = 0.0   # 3D box height (fraction of the frame height); 0 = flat 2D area


@router.put("/zones/{camera_id}")
def save_zone(camera_id: int, body: ZoneIn, principal: dict = Depends(auth.require_admin)):
    cam = _camera(camera_id)
    try:
        zone = visitors.save_zone(camera_id, body.polygon, body.enabled, body.height)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    audit.record("visitors.zone_save", principal, target=f"camera:{camera_id}", points=len(body.polygon), height=body.height, enabled=body.enabled, camera=cam["name"])
    return {"zone": zone, **visitors.service.camera_status(camera_id)}


@router.delete("/zones/{camera_id}")
def clear_zone(camera_id: int, principal: dict = Depends(auth.require_admin)):
    _camera(camera_id)
    removed = visitors.delete_zone(camera_id)
    audit.record("visitors.zone_clear", principal, target=f"camera:{camera_id}", existed=removed)
    return {"cleared": removed, **visitors.service.camera_status(camera_id)}


@router.get("/events")
def list_events(camera_id: int | None = None, after_id: int | None = None, limit: int = Query(50, ge=1, le=200)):
    return {"events": visitors.list_events(camera_id, limit, after_id)}


@router.get("/kpis")
def visitor_kpis():
    """Today's KPI cards (visitors = visits; employees never counted)."""
    return visitors.kpis()


@router.get("/visits")
def list_visits(camera_id: int | None = None, limit: int = Query(100, ge=1, le=2000),
                start: str | None = None, end: str | None = None):
    """Visitor history, one row per visit (repeat alerts grouped). start / end
    (YYYY-MM-DD, both included) limit it to those days."""
    try:
        since = datetime.datetime.combine(datetime.date.fromisoformat(start), datetime.time.min).timestamp() if start else None
        until = (datetime.datetime.combine(datetime.date.fromisoformat(end) + datetime.timedelta(days=1), datetime.time.min).timestamp()
                 if end else None)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must look like 2026-11-03")
    if since is not None and until is not None and since >= until:
        raise HTTPException(status_code=422, detail="The start date must be on or before the end date")
    rows = visitors.list_visits(camera_id, limit, since=since, until=until)
    faces = visitors.faces_during_visits(rows)
    names: dict[str, str] = {}

    def named(ids: dict) -> list[dict]:
        out = []
        for emp, n in sorted(ids.items(), key=lambda x: -x[1]):
            if emp not in names:
                names[emp] = (employee_directory.get_employee(emp) or {}).get("name") or emp
            out.append({"employee_id": emp, "name": names[emp], "faces": n})
        return out

    for r in rows:
        f = faces.get(r["visit"]) or {"employees": {}, "possible": {}, "unknown": 0}
        r["faces_seen"] = {"employees": named(f["employees"]), "possible": named(f["possible"]), "unknown": f["unknown"]}
    return {"visits": rows}


@router.get("/employee-visits")
def list_employee_visits(camera_id: int | None = None, limit: int = Query(100, ge=1, le=500)):
    """Registered employees seen in a visitor area: kept apart from visitors,
    never alerted."""
    rows = visitors.list_employee_visits(camera_id, limit)
    snaps = visitors.employee_stay_snapshots(rows)
    for r in rows:
        emp = employee_directory.get_employee(r["employee_id"]) or {}
        r["name"] = emp.get("name") or r["employee_id"]
        r["department"] = emp.get("company") or ""
        r["person_type"] = "employee"
        # Original face captures of this employee during this stay (read-only).
        r["snapshot_count"] = len(snaps.get(r["id"], []))
        r["snapshot_id"] = snaps[r["id"]][len(snaps[r["id"]]) // 2]["capture_id"] if snaps.get(r["id"]) else None
    return {"visits": rows}


@router.get("/employee-visits/{stay_id}/snapshots")
def employee_visit_snapshots(stay_id: int):
    """The stored face captures of one employee stay in a visitor area."""
    stay = visitors.get_employee_visit(stay_id)
    if stay is None:
        raise HTTPException(status_code=404, detail="Employee visit not found")
    return {"snapshots": visitors.employee_stay_snapshots([stay])[stay_id]}


@router.get("/employee-snapshots/{capture_id}")
def employee_snapshot(capture_id: int):
    path = storage.resolve(visitors.employee_snapshot_path(capture_id))
    if path is None or not path.is_file():
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/events/{event_id}")
def get_event(event_id: int):
    ev = visitors.get_event(event_id)
    if ev is None:
        raise HTTPException(status_code=404, detail="This visitor event no longer exists (events are deleted after their retention period)")
    return ev


@router.get("/events/{event_id}/clip")
def get_clip(event_id: int):
    path, reason = visitors.clip_file(event_id)
    if path is None:
        status, detail = {
            "missing": (404, "Visitor event not found"),
            "expired": (410, "This clip has been deleted (visitor clips are removed automatically)"),
            "pending": (409, "The clip is still being recorded - try again in a few seconds"),
            "failed": (404, "No clip was recorded for this event"),
            "file_missing": (404, "The clip file is missing"),
        }[reason]
        raise HTTPException(status_code=status, detail=detail)
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})


@router.delete("/events/{event_id}")
def delete_event(event_id: int, principal: dict = Depends(auth.require_admin)):
    if not visitors.delete_event(event_id):
        raise HTTPException(status_code=404, detail="Visitor event not found")
    audit.record("visitors.event_delete", principal, target=f"event:{event_id}")
    return {"ok": True, "id": event_id}


@router.get("/settings")
def settings():
    latest = visitors.MAX_PRESENCE_HOURS + visitors.RETENTION_AFTER_DEPARTURE_HOURS
    repeat = visitors.REPEAT_ALERT_MINUTES
    return {
        "entry_confirm_looks": visitors.ENTRY_CONFIRM_LOOKS,
        "exit_grace_seconds": visitors.EXIT_GRACE_SECONDS,
        "absence_grace_seconds": visitors.ABSENCE_GRACE_SECONDS,
        "detection_gap_seconds": visitors.DETECTION_GAP_SECONDS,
        "repeat_alert_minutes": repeat,
        "exclude_employees": visitors.EXCLUDE_EMPLOYEES,
        "employee_sources": sorted(visitors.EMPLOYEE_SOURCES),
        "one_alert_per_visitor": repeat <= 0,
        "clip_pre_seconds": visitors.PRE_SECONDS,
        "clip_post_seconds": visitors.POST_SECONDS,
        "retention_after_departure_hours": visitors.RETENTION_AFTER_DEPARTURE_HOURS,
        "max_presence_hours": visitors.MAX_PRESENCE_HOURS,
        "latest_deletion_hours_after_entry": latest,
        "rules": {
            "new_entry": f"An unknown person who stays in the area for {visitors.DETECTION_GAP_SECONDS:g} seconds raises a visitor alert"
                         + (" - recognised employees never do." if visitors.EXCLUDE_EMPLOYEES else "."),
            "reentry": (f"While they stay, they are alerted again every {repeat:g} minutes (not more often); "
                        if repeat > 0 else "Each visit raises one alert; ")
                       + f"a short loss of sight does not count as leaving. After {visitors.ABSENCE_GRACE_SECONDS:g} seconds "
                         "out of the area the visit ends, and coming back starts a new one.",
            "retention": f"Clips are deleted {visitors.RETENTION_AFTER_DEPARTURE_HOURS:g} h after the visitor left the area; "
                         f"if their departure is never established, at the latest {latest:g} h after they entered.",
        },
    }
