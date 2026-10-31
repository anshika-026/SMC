"""
zone_routes.py

Zones & areas API (admin only):

  GET    /api/zones                          every marked area, all types, with today's activity
  GET    /api/zones/restricted               restricted zones
  POST   /api/zones/restricted               create {camera_id, name, polygon, rule, severity, cooldown_minutes, enabled}
  PUT    /api/zones/restricted/{id}          update any of those
  DELETE /api/zones/restricted/{id}          delete (activity history kept)
  GET    /api/zones/restricted/{id}/activity entries into the zone, newest first

Visitor areas, desk zones, the staff gate line and footfall gates keep their
own endpoints (visitor_routes, desk_routes, staff_routes, footfall_routes).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app import auth, employee_directory, restricted_zones, zones

router = APIRouter(prefix="/api/zones", tags=["zones"], dependencies=[Depends(auth.require_admin)])


@router.get("")
def all_zones():
    return zones.list_all()


@router.get("/restricted")
def restricted_list():
    return {"zones": restricted_zones.list_zones(), "rules": restricted_zones.RULES, "severities": list(restricted_zones.SEVERITIES)}


class RestrictedIn(BaseModel):
    camera_id: int | None = None
    name: str | None = None
    polygon: list[list[float]] | None = None
    rule: str | None = None
    severity: str | None = None
    cooldown_minutes: float | None = None
    enabled: bool | None = None


@router.post("/restricted", status_code=201)
def restricted_create(body: RestrictedIn, principal: dict = Depends(auth.require_admin)):
    if body.camera_id is None:
        raise HTTPException(status_code=422, detail="Choose a camera.")
    try:
        return restricted_zones.create_zone(body.camera_id, body.name, body.polygon or [], body.rule or "non_employees",
                                            body.severity or "High", body.cooldown_minutes or 10,
                                            True if body.enabled is None else body.enabled, principal)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.put("/restricted/{zone_id}")
def restricted_update(zone_id: int, body: RestrictedIn, principal: dict = Depends(auth.require_admin)):
    try:
        return restricted_zones.update_zone(zone_id, body.model_dump(exclude={"camera_id"}), principal)
    except KeyError:
        raise HTTPException(status_code=404, detail="Zone not found")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.delete("/restricted/{zone_id}")
def restricted_delete(zone_id: int, principal: dict = Depends(auth.require_admin)):
    if not restricted_zones.delete_zone(zone_id, principal):
        raise HTTPException(status_code=404, detail="Zone not found")
    return {"ok": True}


@router.get("/restricted/{zone_id}/activity")
def restricted_activity(zone_id: int, limit: int = Query(100, ge=1, le=500)):
    rows = restricted_zones.activity(zone_id, limit=limit)
    for r in rows:
        r["name"] = (employee_directory.get_display(r["employee_id"])[0] or r["employee_id"]) if r["employee_id"] else "Person (not recognised)"
    return {"activity": rows}
