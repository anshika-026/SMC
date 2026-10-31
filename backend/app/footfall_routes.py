"""
footfall_routes.py

Unique-footfall reporting across all entry gates (see footfall.py), plus
the UAT panel's reset / people / snapshot endpoints.

Mount with: app.include_router(footfall_routes.router) in main.py
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import analytics_settings, audit, auth, camera_db, camera_stream
from app.footfall import service
from app.reid import reid_db

router = APIRouter(prefix="/api/footfall", tags=["footfall"])


@router.get("/summary")
def footfall_summary(_: dict = Depends(auth.require_admin)):
    """Today's unique people across every gate (each person once, whichever
    gate(s) they used), plus the per-gate breakdown, hourly arrivals vs
    yesterday, and today's visitor list."""
    return service.summary()


@router.get("/history")
def footfall_history(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to"),
                     _: dict = Depends(auth.require_admin)):
    """Daily entries / exits / employee entries for a date range (max 366 days),
    including events kept from before a count reset."""
    import datetime

    try:
        a, b = datetime.date.fromisoformat(date_from), datetime.date.fromisoformat(date_to)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must look like 2026-10-31")
    if b < a or (b - a).days > 366:
        raise HTTPException(status_code=422, detail="Pick a range of up to one year")
    return {"days": reid_db.footfall_daily(a.isoformat(), b.isoformat())}


@router.get("/day")
def footfall_day(date: str, _: dict = Depends(auth.require_admin)):
    """One day: entries, exits, employee vs visitor entries, arrivals per hour and the peak hour."""
    import datetime

    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must look like 2026-10-31")
    return {"date": date, **service.day_stats(date)}


@router.get("/people")
def footfall_people(_: dict = Depends(auth.require_admin)):
    """Every unique person currently counted, newest first, with snapshot ids."""
    return service.people()


@router.get("/snapshots/{snapshot_id}")
def footfall_snapshot(snapshot_id: int, _: dict = Depends(auth.require_admin)):
    path = service.snapshot_path(snapshot_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/clips/{clip_id}")
def footfall_clip(clip_id: int, _: dict = Depends(auth.require_admin)):
    """The ~10 s clip of a counted person (5 s before, 5 s after they were
    first counted that day). Supports Range, so it plays and seeks."""
    path, reason = service.clip_file(clip_id)
    if path is None:
        status, detail = {
            "missing": (404, "Clip not found"),
            "pending": (409, "The clip is still being recorded - try again in a few seconds"),
            "expired": (410, "This clip has been deleted (footfall clips are removed after 24 hours)"),
            "failed": (404, "No clip was recorded for this person"),
            "file_missing": (404, "The clip file is missing"),
        }[reason]
        raise HTTPException(status_code=status, detail=detail)
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})


@router.post("/reset")
def footfall_reset(_: dict = Depends(auth.require_admin)):
    """UAT: restart the unique count from zero. Deletes every Re-ID identity
    and snapshot; cameras, faces and attendance are untouched."""
    return service.reset()


class ZoneIn(BaseModel):
    # Polygon corners as [x, y] fractions of the frame (0..1); null = whole frame.
    roi: list[list[float]] | None = None


@router.get("/cameras/{camera_id}/frame")
def footfall_camera_frame(camera_id: int, _: dict = Depends(auth.require_admin)):
    """Latest still from a gate camera, for drawing its counting zone on."""
    jpeg = camera_stream.grab_still(camera_id)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="No frame yet — is this camera streaming?")
    return Response(content=jpeg, media_type="image/jpeg")


@router.put("/cameras/{camera_id}/zone")
def footfall_set_zone(camera_id: int, payload: ZoneIn, _: dict = Depends(auth.require_admin)):
    """Only people whose body centre is inside this zone are counted at
    this gate — draw it over the doorway to keep seating areas out."""
    roi = payload.roi
    if roi is not None:
        if len(roi) < 3 or any(len(p) != 2 or not (0 <= p[0] <= 1 and 0 <= p[1] <= 1) for p in roi):
            raise HTTPException(status_code=422, detail="Zone needs at least 3 [x, y] points between 0 and 1")
    service.set_zone(camera_id, roi)
    return {"camera_id": camera_id, "roi": service.get_zone(camera_id)}


GATE_PURPOSE = "Entry/Exit"


def _checked_roi(roi):
    if roi is not None and (len(roi) < 3 or any(len(p) != 2 or not (0 <= p[0] <= 1 and 0 <= p[1] <= 1) for p in roi)):
        raise HTTPException(status_code=422, detail="The area needs at least 3 [x, y] points between 0 and 1")
    return roi


@router.post("/cameras/{camera_id}")
def footfall_add_camera(camera_id: int, payload: ZoneIn, principal: dict = Depends(auth.require_admin)):
    """Adds a camera to footfall with its counting area and starts counting.
    The camera is marked as a gate (purpose "Entry/Exit", which is what
    footfall counts on), the area is saved, and the Unique-footfall switch is
    turned on so counting actually runs. Calling it again just moves the area."""
    cam = camera_db.get_camera(camera_id)
    if cam is None or not cam.get("host"):
        raise HTTPException(status_code=404, detail="Camera not found or not configured")
    roi = _checked_roi(payload.roi)
    was_gate = "entry" in (cam.get("purpose") or "").lower()
    if not was_gate:
        camera_db.update_camera(camera_id, purpose=GATE_PURPOSE)
    service.set_zone(camera_id, roi)
    analytics_settings.set_enabled("footfall", True)
    try:
        service._sync_gates()  # noqa: SLF001 - start counting now instead of at the next 15 s sync
    except Exception:
        pass
    audit.record("footfall.camera_add", principal, target=f"camera:{camera_id}", area=bool(roi), was_gate=was_gate)
    return {"camera_id": camera_id, "name": cam["name"], "roi": service.get_zone(camera_id), "counting": True}


class EntryIn(BaseModel):
    # The line across the gate, [[x1, y1], [x2, y2]] fractions of the frame
    # (where feet cross), and which side is inside (+1 / -1). null line =
    # count everyone in the area again.
    entry_line: list[list[float]] | None = None
    inside_sign: int = 1


@router.put("/cameras/{camera_id}/entry")
def footfall_set_entry(camera_id: int, payload: EntryIn, principal: dict = Depends(auth.require_admin)):
    """With an entry line, only people who cross it from outside to inside
    are counted (and get a clip and a name); people walking past or standing
    outside are not."""
    if camera_db.get_camera(camera_id) is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    line = payload.entry_line
    if line is not None:
        if len(line) != 2 or any(len(p) != 2 or not (0 <= p[0] <= 1 and 0 <= p[1] <= 1) for p in line):
            raise HTTPException(status_code=422, detail="The entry line needs 2 [x, y] points between 0 and 1")
        if abs(line[0][0] - line[1][0]) + abs(line[0][1] - line[1][1]) < 0.02:
            raise HTTPException(status_code=422, detail="The entry line is too short - click two points across the gate")
    service.set_entry_line(camera_id, line, 1 if payload.inside_sign >= 0 else -1)
    audit.record("footfall.entry_line", principal, target=f"camera:{camera_id}", line=bool(line))
    return {"camera_id": camera_id, **service.get_entry(camera_id)}


@router.delete("/cameras/{camera_id}")
def footfall_remove_camera(camera_id: int, principal: dict = Depends(auth.require_admin)):
    """Stops counting at this camera: back to purpose "General", counting area
    cleared. People already counted stay in today's numbers."""
    cam = camera_db.get_camera(camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    camera_db.update_camera(camera_id, purpose="General")
    service.set_zone(camera_id, None)
    try:
        service._sync_gates()  # noqa: SLF001
    except Exception:
        pass
    audit.record("footfall.camera_remove", principal, target=f"camera:{camera_id}")
    return {"camera_id": camera_id, "removed": True}
