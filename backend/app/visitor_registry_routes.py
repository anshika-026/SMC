"""
visitor_registry_routes.py

Visitor records API (admin only; see visitor_registry.py for the rules).

  GET    /api/visitor-records/settings                 retention, long-stay threshold, visitor notice
  PUT    /api/visitor-records/settings
  GET    /api/visitor-records/hosts                    employees a visitor can come to meet
  GET    /api/visitor-records/cameras                  what each camera can detect (entry / exit)
  POST   /api/visitor-records/match                    registered visitors who may be the same person
  POST   /api/visitor-records/visits                   register a visit (409 + matches when a likely duplicate)
  GET    /api/visitor-records/visits                   ?from= &to= &status= &host= &department= &type=first|repeat &q=
  GET    /api/visitor-records/visits/{id}              one visit with its events and media
  POST   /api/visitor-records/visits/{id}/times        record entry | exit | host_notified | meeting_start
  POST   /api/visitor-records/visits/{id}/correct      correct (or clear) a time, with a reason
  POST   /api/visitor-records/visits/{id}/cancel
  DELETE /api/visitor-records/visits/{id}
  GET    /api/visitor-records/visits/{id}/candidates   camera detections that may be this visitor's entry / exit
  POST   /api/visitor-records/visits/{id}/link         operator confirms a detection {ref}
  POST   /api/visitor-records/visits/{id}/snapshot     take a snapshot now from a camera {camera_id}
  GET    /api/visitor-records/detections?day=          the day's camera entries / exits, linked or not
  GET    /api/visitor-records/media/{id}               a snapshot (JPEG) or clip (MP4)
  DELETE /api/visitor-records/media/{id}
  GET    /api/visitor-records/visitors                 ?q= &day=
  GET    /api/visitor-records/visitors/{id}            profile + visit history + media
  PATCH  /api/visitor-records/visitors/{id}
  DELETE /api/visitor-records/visitors/{id}            erase the visitor and everything recorded about them
  GET    /api/visitor-records/kpis?day=
  GET    /api/visitor-records/analytics?from=&to=
  GET    /api/visitor-records/export?format=csv|xlsx&from=&to=&...
"""

from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app import auth, face_db
from app import visitor_registry as reg

router = APIRouter(prefix="/api/visitor-records", tags=["visitor-records"], dependencies=[Depends(auth.require_admin)])


def _bad(e: Exception):
    raise HTTPException(status_code=422, detail=str(e))


def _visit_or_404(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError:
        raise HTTPException(status_code=404, detail="Visit not found")
    except reg.RegistryError as e:
        _bad(e)


@router.get("/settings")
def get_settings():
    return reg.get_settings()


class SettingsIn(BaseModel):
    media_retention_days: int | None = None
    long_stay_minutes: int | None = None
    waiting_alert_minutes: int | None = None
    default_gate_camera_id: int | None = None
    notice_text: str | None = None


@router.put("/settings")
def put_settings(body: SettingsIn, principal: dict = Depends(auth.require_admin)):
    try:
        return reg.set_settings(body.model_dump(exclude_unset=True), principal)
    except reg.RegistryError as e:
        _bad(e)


@router.get("/hosts")
def hosts():
    rows = face_db.list_employees()
    return {"hosts": sorted(({"employee_id": str(r["employee_id"]), "name": r["name"], "department": r.get("department") or None}
                             for r in rows), key=lambda h: h["name"].lower())}


@router.get("/cameras")
def cameras():
    return {"cameras": reg.camera_capabilities()}


class MatchIn(BaseModel):
    name: str | None = None
    mobile: str | None = None
    email: str | None = None
    company: str | None = None


@router.post("/match")
def match(body: MatchIn):
    return {"matches": reg.find_matches(body.name, body.mobile, body.email, body.company)}


class VisitIn(BaseModel):
    name: str | None = None
    purpose: str
    host_employee_id: str | None = None
    host_name: str | None = None
    visit_date: str | None = None
    company: str | None = None
    mobile: str | None = None
    email: str | None = None
    notes: str | None = None
    gate_camera_id: int | None = None
    snapshot_consent: bool = False
    visitor_id: int | None = None
    create_new: bool = False


@router.post("/visits", status_code=201)
def register(body: VisitIn, principal: dict = Depends(auth.require_admin)):
    try:
        return reg.register_visit(body.model_dump(), principal)
    except reg.DuplicateVisitor as e:
        return JSONResponse(status_code=409, content={"detail": str(e), "matches": e.matches})
    except reg.RegistryError as e:
        _bad(e)


@router.get("/visits")
def visits(date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
           status: str | None = None, host: str | None = None, department: str | None = None,
           type: str | None = None, q: str | None = None, limit: int = Query(500, ge=1, le=5000)):
    try:
        return {"visits": reg.list_visits(date_from, date_to, status, host, department, type, q, limit=limit)}
    except reg.RegistryError as e:
        _bad(e)


@router.get("/visits/{visit_id}")
def visit(visit_id: int):
    return _visit_or_404(reg.get_visit, visit_id, with_detail=True)


class TimeIn(BaseModel):
    kind: str
    ts: float | None = None
    note: str | None = None


@router.post("/visits/{visit_id}/times")
def record_time(visit_id: int, body: TimeIn, principal: dict = Depends(auth.require_admin)):
    return _visit_or_404(reg.record_time, visit_id, body.kind, body.ts, principal, body.note)


class CorrectIn(BaseModel):
    kind: str
    ts: float | None = None
    note: str


@router.post("/visits/{visit_id}/correct")
def correct(visit_id: int, body: CorrectIn, principal: dict = Depends(auth.require_admin)):
    return _visit_or_404(reg.correct_time, visit_id, body.kind, body.ts, body.note, principal)


class NoteIn(BaseModel):
    note: str | None = None


@router.post("/visits/{visit_id}/cancel")
def cancel(visit_id: int, body: NoteIn | None = None, principal: dict = Depends(auth.require_admin)):
    return _visit_or_404(reg.cancel_visit, visit_id, principal, body.note if body else None)


@router.delete("/visits/{visit_id}")
def delete_visit(visit_id: int, principal: dict = Depends(auth.require_admin)):
    if not reg.delete_visit(visit_id, principal):
        raise HTTPException(status_code=404, detail="Visit not found")
    return {"ok": True}


@router.get("/visits/{visit_id}/candidates")
def visit_candidates(visit_id: int):
    return _visit_or_404(reg.candidates, visit_id)


class LinkIn(BaseModel):
    ref: str


@router.post("/visits/{visit_id}/link")
def link(visit_id: int, body: LinkIn, principal: dict = Depends(auth.require_admin)):
    return _visit_or_404(reg.link_detection, visit_id, body.ref, principal)


class SnapshotIn(BaseModel):
    camera_id: int | None = None


@router.post("/visits/{visit_id}/snapshot")
def snapshot(visit_id: int, body: SnapshotIn, principal: dict = Depends(auth.require_admin)):
    return _visit_or_404(reg.capture_live, visit_id, body.camera_id, principal)


@router.get("/detections")
def detections(day: str | None = None, kind: str | None = None):
    try:
        return {"detections": reg.detections(day, kind), "cameras": reg.camera_capabilities()}
    except reg.RegistryError as e:
        _bad(e)


@router.get("/media/{media_id}")
def media(media_id: int):
    path, ctype = reg.media_file(media_id)
    if path is None:
        raise HTTPException(status_code=404, detail="This photo/clip is no longer available (it may have passed its retention period)")
    return FileResponse(path, media_type=ctype, headers={"Cache-Control": "private, no-store"})


@router.delete("/media/{media_id}")
def delete_media(media_id: int, principal: dict = Depends(auth.require_admin)):
    if not reg.delete_media(media_id, principal):
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@router.get("/visitors")
def visitors(q: str | None = None, day: str | None = None, limit: int = Query(100, ge=1, le=1000)):
    try:
        return {"visitors": reg.search_visitors(q, day, limit)}
    except reg.RegistryError as e:
        _bad(e)


@router.get("/visitors/{visitor_id}")
def profile(visitor_id: int):
    try:
        return reg.get_profile(visitor_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Visitor not found")


class ProfileIn(BaseModel):
    name: str | None = None
    company: str | None = None
    mobile: str | None = None
    email: str | None = None
    notes: str | None = None


@router.patch("/visitors/{visitor_id}")
def update_profile(visitor_id: int, body: ProfileIn, principal: dict = Depends(auth.require_admin)):
    try:
        return reg.update_visitor(visitor_id, body.model_dump(exclude_unset=True), principal)
    except KeyError:
        raise HTTPException(status_code=404, detail="Visitor not found")
    except reg.RegistryError as e:
        _bad(e)


@router.delete("/visitors/{visitor_id}")
def erase_visitor(visitor_id: int, principal: dict = Depends(auth.require_admin)):
    if not reg.delete_visitor(visitor_id, principal):
        raise HTTPException(status_code=404, detail="Visitor not found")
    return {"ok": True}


@router.get("/kpis")
def kpis(day: str | None = None):
    try:
        return reg.kpis(day)
    except reg.RegistryError as e:
        _bad(e)


@router.get("/analytics")
def analytics(date_from: str = Query(..., alias="from"), date_to: str = Query(..., alias="to")):
    try:
        return reg.analytics(date_from, date_to)
    except reg.RegistryError as e:
        _bad(e)


@router.get("/export")
def export(format: str = "csv", date_from: str | None = Query(None, alias="from"), date_to: str | None = Query(None, alias="to"),
           status: str | None = None, host: str | None = None, department: str | None = None, type: str | None = None,
           q: str | None = None):
    try:
        header, rows = reg.export_rows(date_from=date_from, date_to=date_to, status=status, host=host,
                                       department=department, visitor_type=type, q=q)
    except reg.RegistryError as e:
        _bad(e)
    stamp = f"{date_from or 'all'}_to_{date_to or datetime.date.today().isoformat()}"
    if format == "xlsx":
        return Response(reg.to_xlsx(header, rows), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="visitor-records_{stamp}.xlsx"'})
    if format != "csv":
        raise HTTPException(status_code=422, detail="format must be csv or xlsx")
    return Response(reg.to_csv(header, rows), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="visitor-records_{stamp}.csv"'})
