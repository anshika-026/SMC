"""
intrusion_routes.py

Restricted-area / loitering zones, intrusion alerts, stats and analytics (see
intrusion.py). Ported from MAINDV (intrusion-loitering); in SMC every route is
for a signed-in operator (auth.require_admin) and the whole router needs the
Master Portal license feature "intrusion_detection" (main.py).

  GET    /api/intrusion/zones                    zones with today's activity
  POST   /api/intrusion/zones                    create (one camera's outline)
  PATCH  /api/intrusion/zones/{id}               update any setting / the outline
  DELETE /api/intrusion/zones/{id}               delete (alerts and history kept)
  GET    /api/intrusion/stats                    last 7 days + today per zone (MAINDV)
  GET    /api/intrusion/events?start&end         itemized detections (MAINDV)
  GET    /api/intrusion/alerts?start&end&...     intrusion + loitering alerts with clip state
  GET    /api/intrusion/analytics?start&end&...  totals, by camera / zone / area / hour / day
  GET    /api/intrusion/export?start&end         .xlsx of the alerts

Mount with: app.include_router(intrusion_routes.router) in main.py
"""

import datetime
import re

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel

from app import auth, camera_db, intrusion

router = APIRouter(prefix="/api/intrusion", tags=["intrusion"], dependencies=[Depends(auth.require_admin)])

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_RANGE_DAYS = 366

# Smallest zone worth saving, as a fraction of the whole picture (0.2%). Corners in a straight
# line, or a speck, can never contain a person's feet.
MIN_ZONE_AREA = 0.002


def _parse_range(start: str | None, end: str | None) -> tuple[float, float, datetime.date, datetime.date]:
    """YYYY-MM-DD dates (default: the last 7 days) -> epoch range covering both whole days."""
    today = datetime.date.today()
    try:
        end_d = datetime.date.fromisoformat(end) if end else today
        start_d = datetime.date.fromisoformat(start) if start else end_d - datetime.timedelta(days=6)
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must look like 2026-11-02")
    if start_d > end_d:
        raise HTTPException(status_code=422, detail="The start date is after the end date")
    if (end_d - start_d).days >= MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail="Choose a range of at most a year")
    return (datetime.datetime.combine(start_d, datetime.time.min).timestamp(),
            datetime.datetime.combine(end_d, datetime.time.max).timestamp(), start_d, end_d)


def _polygon_area(polygon: list[list[float]]) -> float:
    """Area of a polygon of [x, y] fractions (shoelace formula)."""
    total = 0.0
    for i, (x1, y1) in enumerate(polygon):
        x2, y2 = polygon[(i + 1) % len(polygon)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def _check_polygon(polygon: list[list[float]]) -> None:
    if len(polygon) < 3 or any(len(p) != 2 or not (0 <= p[0] <= 1 and 0 <= p[1] <= 1) for p in polygon):
        raise HTTPException(status_code=422, detail="A zone needs at least 3 corner points inside the picture")
    if _polygon_area(polygon) < MIN_ZONE_AREA:
        raise HTTPException(status_code=422, detail="The zone is too small, or its corners are in a straight line. Draw a larger area")


def _check_zone_settings(zone_type: str, dwell_seconds, grace_seconds, severity) -> None:
    """Zone type and the alert settings that go with it."""
    if zone_type not in intrusion.ZONE_TYPES:
        raise HTTPException(status_code=422, detail=f"Zone type must be one of: {', '.join(intrusion.ZONE_TYPES)}")
    if severity is not None and severity not in intrusion.SEVERITIES:
        raise HTTPException(status_code=422, detail=f"Alert severity must be one of: {', '.join(intrusion.SEVERITIES)}")
    if zone_type == "loitering" and (
            dwell_seconds is None or not intrusion.LOITER_MIN_SECONDS <= dwell_seconds <= intrusion.LOITER_MAX_SECONDS):
        raise HTTPException(status_code=422, detail="A loitering zone needs a duration between 5 seconds and 24 hours")
    if grace_seconds is not None and not 0 <= grace_seconds <= 300:
        raise HTTPException(status_code=422, detail="The grace period must be between 0 and 300 seconds")


def _check_gate_line(line) -> None:
    """An entry gate's line: 2 points inside the picture, long enough to cross."""
    if line is None:
        return
    if len(line) != 2 or any(len(p) != 2 or not (0 <= p[0] <= 1 and 0 <= p[1] <= 1) for p in line):
        raise HTTPException(status_code=422, detail="The gate line needs 2 points inside the picture")
    (x1, y1), (x2, y2) = line
    if ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5 < 0.03:
        raise HTTPException(status_code=422, detail="The gate line is too short. Click two points across the gate")


def _check_window(start, end):
    if (start is None) != (end is None) or any(v is not None and not _HHMM.match(v) for v in (start, end)):
        raise HTTPException(status_code=422, detail="Active hours need both a start and an end, like 19:00 and 08:00")


@router.get("/zones")
def list_zones(camera_id: int | None = None):
    zones = intrusion.list_zones(camera_id)
    names = {c["id"]: c["name"] for c in camera_db.list_cameras()}
    st = intrusion.stats()
    for z in zones:
        z["camera_name"] = names.get(z["camera_id"], f"Camera {z['camera_id']}")
        z["active_now"] = intrusion.is_active(z)
        z["intrusions_today"] = st["per_zone_today"].get(z["id"], 0)
        z["last_intrusion"] = st["last_seen"].get(z["id"])
    return zones


class ZoneIn(BaseModel):
    camera_id: int
    name: str
    polygon: list[list[float]]
    active_from: str | None = None
    active_to: str | None = None
    zone_type: str = "restricted"         # "restricted" (original behaviour) or "loitering"
    dwell_seconds: int | None = None      # loitering only: how long a person must stay inside
    grace_seconds: float | None = None    # loitering only: how long a gap in detection is forgiven
    severity: str | None = None           # None = the type's default (High for restricted, Medium for loitering)
    alerts_enabled: bool = True
    area: str | None = None               # SMC: the area the zone belongs to, e.g. "Reception"
    is_entry_gate: bool = False           # SMC: this zone is an entry gate, e.g. "Reception Entry Gate"
    entry_line: list[list[float]] | None = None   # SMC, entry gates: people crossing it towards inside are marked
    inside_sign: int = 1                  # which side of the line is inside (the editor's arrow)


@router.post("/zones")
def create_zone(payload: ZoneIn):
    if camera_db.get_camera(payload.camera_id) is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="Give the zone a name")
    _check_polygon(payload.polygon)
    _check_window(payload.active_from or None, payload.active_to or None)
    _check_zone_settings(payload.zone_type, payload.dwell_seconds, payload.grace_seconds, payload.severity)
    _check_gate_line(payload.entry_line)
    if payload.entry_line and (not payload.is_entry_gate or payload.zone_type != "restricted"):
        raise HTTPException(status_code=422, detail="Only an entry gate (not a loitering zone) has a gate line")
    return intrusion.create_zone(
        payload.camera_id, payload.name.strip(), payload.polygon, payload.active_from or None, payload.active_to or None,
        zone_type=payload.zone_type, dwell_seconds=payload.dwell_seconds if payload.zone_type == "loitering" else None,
        grace_seconds=payload.grace_seconds if payload.zone_type == "loitering" else None,
        severity=payload.severity, alerts_enabled=payload.alerts_enabled,
        area=(payload.area or "").strip() or None, is_entry_gate=payload.is_entry_gate,
        entry_line=payload.entry_line if payload.is_entry_gate else None, inside_sign=payload.inside_sign)


class ZoneUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    active_from: str | None = None
    active_to: str | None = None
    polygon: list[list[float]] | None = None
    zone_type: str | None = None
    dwell_seconds: int | None = None
    grace_seconds: float | None = None
    severity: str | None = None
    alerts_enabled: bool | None = None
    area: str | None = None
    is_entry_gate: bool | None = None
    entry_line: list[list[float]] | None = None
    inside_sign: int | None = None


@router.patch("/zones/{zone_id}")
def update_zone(zone_id: int, payload: ZoneUpdate):
    existing = intrusion.get_zone(zone_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Zone not found")
    fields = payload.model_dump(exclude_unset=True)
    if "active_from" in fields or "active_to" in fields:
        fields["active_from"] = fields.get("active_from") or None
        fields["active_to"] = fields.get("active_to") or None
        _check_window(fields["active_from"], fields["active_to"])
    if "name" in fields and not (fields["name"] or "").strip():
        raise HTTPException(status_code=422, detail="Give the zone a name")
    if "name" in fields:
        fields["name"] = fields["name"].strip()
    if fields.get("polygon") is not None:
        _check_polygon(fields["polygon"])
    elif "polygon" in fields:
        del fields["polygon"]                                  # null means "leave the outline alone"
    if "zone_type" in fields and fields["zone_type"] is None:
        del fields["zone_type"]
    if "is_entry_gate" in fields and fields["is_entry_gate"] is None:
        del fields["is_entry_gate"]
    if "entry_line" in fields:
        _check_gate_line(fields["entry_line"])
    if "inside_sign" in fields and fields["inside_sign"] is None:
        del fields["inside_sign"]
    merged = {**existing, **fields}
    if merged.get("entry_line") and (not merged.get("is_entry_gate") or merged["zone_type"] != "restricted"):
        raise HTTPException(status_code=422, detail="Only an entry gate (not a loitering zone) has a gate line")
    _check_zone_settings(merged["zone_type"], merged.get("dwell_seconds"), merged.get("grace_seconds"), merged.get("severity"))
    if merged["zone_type"] != "loitering":
        fields.update(dwell_seconds=None, grace_seconds=None)  # only loitering zones carry a duration
    zone = intrusion.update_zone(zone_id, **fields)
    if zone is None:
        raise HTTPException(status_code=404, detail="Zone not found")
    return zone


@router.delete("/zones/{zone_id}")
def delete_zone(zone_id: int):
    if intrusion.get_zone(zone_id) is not None:
        intrusion.delete_zone(zone_id)
    return {"ok": True}


@router.get("/stats")
def intrusion_stats():
    return intrusion.stats()


@router.get("/events")
def intrusion_events(start: str | None = None, end: str | None = None):
    """Itemized intrusion detections over a date range (the per-detection feed; alerts dedupe
    repeat detections while an intrusion is open)."""
    start_ts, end_ts, _s, _e = _parse_range(start, end)
    return intrusion.events_list(start_ts, end_ts)


def _check_type(alert_type: str | None) -> str | None:
    if alert_type and alert_type not in intrusion.ALERT_TYPES:
        raise HTTPException(status_code=422, detail=f"type must be one of: {', '.join(intrusion.ALERT_TYPES)}")
    return alert_type or None


@router.get("/alerts")
def intrusion_alerts(start: str | None = None, end: str | None = None, camera_id: int | None = None,
                     zone_id: int | None = None, status: str | None = None, q: str = Query("", max_length=100),
                     type: str | None = None):
    if status and status not in ("Active", "Acknowledged", "Resolved"):
        raise HTTPException(status_code=422, detail="status must be Active, Acknowledged or Resolved")
    start_ts, end_ts, _s, _e = _parse_range(start, end)
    return {"alerts": intrusion.list_alerts(start_ts, end_ts, camera_id=camera_id, zone_id=zone_id, status=status or None, q=q,
                                            alert_type=_check_type(type))}


@router.get("/analytics")
def intrusion_analytics(start: str | None = None, end: str | None = None, camera_id: int | None = None,
                        zone_id: int | None = None, type: str | None = None):
    start_ts, end_ts, start_d, end_d = _parse_range(start, end)
    return {"start": start_d.isoformat(), "end": end_d.isoformat(),
            **intrusion.analytics(start_ts, end_ts, camera_id=camera_id, zone_id=zone_id, alert_type=_check_type(type))}


@router.get("/export")
def intrusion_export(start: str | None = None, end: str | None = None, camera_id: int | None = None,
                     type: str | None = None):
    from app import visitor_registry   # SMC's existing minimal .xlsx writer

    start_ts, end_ts, start_d, end_d = _parse_range(start, end)
    rows = intrusion.list_alerts(start_ts, end_ts, camera_id=camera_id, limit=100_000, alert_type=_check_type(type))
    header = ["Time", "Event", "Camera", "Area", "Zone", "Entry gate", "Duration (s)", "Severity", "Status", "Clip", "Message"]
    data = [[datetime.datetime.fromtimestamp(r["ts"]).strftime("%Y-%m-%d %H:%M:%S"), r["event"], r["camera_name"], r["area"],
             r["zone_name"], "Yes" if r["is_entry_gate"] else "", round(r["duration_seconds"]), r["severity"], r["status"],
             r["clip_status"] or "", r["message"]] for r in rows]
    return Response(visitor_registry.to_xlsx(header, data, sheet="Intrusion"),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="intrusion_{start_d}_{end_d}.xlsx"'})
