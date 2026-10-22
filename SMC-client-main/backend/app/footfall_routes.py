"""
footfall_routes.py

Unique-footfall reporting for the SMC cameras (see footfall.py), plus the
UAT panel's reset / people / snapshot endpoints and per-camera counting
zones. Same contract as MAINDV's /api/footfall/*.
"""

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import config
from app.footfall import service


def require_token(authorization: str | None = Header(default=None)) -> None:
    """No-op unless API_TOKEN is set; then a matching Bearer token is required."""
    if not config.API_TOKEN:
        return
    token = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, config.API_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


router = APIRouter(prefix="/api/footfall", tags=["footfall"], dependencies=[Depends(require_token)])


def _known_camera(camera_id: int) -> None:
    if camera_id not in {c["id"] for c in config.cameras()}:
        raise HTTPException(status_code=404, detail="Unknown camera")


@router.get("/summary")
def footfall_summary():
    """Today's unique people across both cameras (each person once), plus
    the per-camera breakdown, hourly arrivals vs yesterday, and today's
    visitor list."""
    return service.summary()


@router.get("/people")
def footfall_people():
    """Every unique person currently counted, newest first, with snapshot ids."""
    return service.people()


@router.get("/snapshots/{snapshot_id}")
def footfall_snapshot(snapshot_id: int):
    path = service.snapshot_path(snapshot_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Snapshot not found")
    return FileResponse(path, media_type="image/jpeg")


@router.post("/reset")
def footfall_reset():
    """UAT: restart the unique count from zero. Deletes every Re-ID identity
    and snapshot."""
    return service.reset()


class ZoneIn(BaseModel):
    # Polygon corners as [x, y] fractions of the frame (0..1); null = whole frame.
    roi: list[list[float]] | None = None


@router.get("/cameras/{camera_id}/frame")
def footfall_camera_frame(camera_id: int):
    """Latest still from a camera, for drawing its counting zone on."""
    _known_camera(camera_id)
    jpeg = service.frame_jpeg(camera_id)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="No frame yet — is this camera streaming?")
    return Response(content=jpeg, media_type="image/jpeg")


@router.put("/cameras/{camera_id}/zone")
def footfall_set_zone(camera_id: int, payload: ZoneIn):
    """Only people whose body centre is inside this zone are counted on
    this camera — draw it over the doorway to keep seating areas out."""
    _known_camera(camera_id)
    roi = payload.roi
    if roi is not None:
        if len(roi) < 3 or any(len(p) != 2 or not (0 <= p[0] <= 1 and 0 <= p[1] <= 1) for p in roi):
            raise HTTPException(status_code=422, detail="Zone needs at least 3 [x, y] points between 0 and 1")
    service.set_zone(camera_id, roi)
    return {"camera_id": camera_id, "roi": service.get_zone(camera_id)}
