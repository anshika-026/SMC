"""
zone_routes.py

Zones & areas API (admin only):

  GET    /api/zones                          every marked area, all types, with today's activity

Each zone type keeps its own endpoints: visitor areas (visitor_routes), desk
zones (desk_routes), the staff gate line (staff_routes), footfall gates
(footfall_routes) and restricted / loitering / entry-gate zones
(intrusion_routes, the intrusion engine ported from MAINDV).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import auth, zones

router = APIRouter(prefix="/api/zones", tags=["zones"], dependencies=[Depends(auth.require_admin)])


@router.get("")
def all_zones():
    return zones.list_all()
