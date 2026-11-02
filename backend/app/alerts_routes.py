"""
alerts_routes.py

Alerts & Events (see alerts.py).

Mount with: app.include_router(alerts_routes.router) in main.py
"""

import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import alerts, auth

router = APIRouter(prefix="/api/alerts", tags=["alerts"])

RANGES = {"today", "yesterday", "week", "all"}


def _who(principal: dict) -> str:
    return principal.get("email") or "Admin"


@router.get("")
def list_alerts(range: str = "today", _: dict = Depends(auth.require_admin)):
    if range not in RANGES:
        raise HTTPException(status_code=422, detail=f"range must be one of {sorted(RANGES)}")
    return alerts.list_alerts(range)


@router.get("/summary")
def alerts_summary(_: dict = Depends(auth.require_admin)):
    return alerts.summary()


@router.get("/{alert_id}/snapshot")
def alert_snapshot(alert_id: int, _: dict = Depends(auth.require_admin)):
    path = alerts.snapshot_path(alert_id)
    if path is None:
        raise HTTPException(status_code=404, detail="No snapshot for this alert")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/{alert_id}/clip")
def alert_clip(alert_id: int, download: bool = False, _: dict = Depends(auth.require_admin)):
    """The evidence video for an intrusion / loitering alert (see evidence.py). Never returns
    anything but the alert's own finished clip; every other state is an explicit message, not an
    empty video."""
    info = alerts.clip_info(alert_id)
    if info is None or info["status"] is None:
        raise HTTPException(status_code=404, detail="No clip for this alert")
    if info["status"] == "pending":
        raise HTTPException(status_code=409, detail="The clip is still being prepared. Try again in a few seconds.")
    if info["status"] == "unavailable":
        raise HTTPException(status_code=404, detail="No video was available from the camera around this event.")
    if info["status"] == "expired":
        raise HTTPException(status_code=404, detail="This clip was removed after its retention period.")
    if info["status"] != "ready" or info["path"] is None:
        raise HTTPException(status_code=404, detail="The clip could not be saved.")
    if not download:
        return FileResponse(info["path"], media_type="video/mp4")
    label = re.sub(r"[^A-Za-z0-9]+", "-", f"{info['event']} {info['zone_name'] or ''}").strip("-").lower()
    return FileResponse(info["path"], media_type="video/mp4", filename=f"{label}-alert-{alert_id}.mp4",
                        content_disposition_type="attachment")


@router.post("/{alert_id}/acknowledge")
def acknowledge(alert_id: int, principal: dict = Depends(auth.require_admin)):
    return {"ok": alerts.acknowledge(alert_id, _who(principal))}


class ResolveIn(BaseModel):
    reason: str


@router.post("/{alert_id}/resolve")
def resolve(alert_id: int, payload: ResolveIn, principal: dict = Depends(auth.require_admin)):
    if not payload.reason.strip():
        raise HTTPException(status_code=422, detail="Pick a reason for resolving")
    return {"ok": alerts.resolve(alert_id, _who(principal), payload.reason.strip())}
