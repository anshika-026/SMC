"""
whatsapp_routes.py

SMC Connect (WhatsApp Business) API, see whatsapp.py. Everything is for a signed-in operator
except the Meta webhook, which Meta calls and which is checked by its signature instead.

  GET    /api/connect/overview                 connection, stats, last success
  POST   /api/connect/connect                  {country_code, number}: wizard step 1 (pending)
  POST   /api/connect/verify                   ask Meta; connected only if Meta confirms
  POST   /api/connect/disconnect
  GET    /api/connect/recipients               list / POST add / PATCH {id} / DELETE {id}
  GET    /api/connect/rules                    rules with template + preview
  PATCH  /api/connect/rules/{key}              enabled, recipients, priority, preferences, template
  POST   /api/connect/preview                  {key, template}: preview with sample values
  POST   /api/connect/test                     {recipient_id | country_code+number, event_key}
  GET    /api/connect/history?q&event&status&start&end
  GET    /api/connect/webhook                  Meta's verification handshake (public)
  POST   /api/connect/webhook                  Meta's delivery statuses (public, signed)

Mount with: app.include_router(whatsapp_routes.router) and whatsapp_routes.webhook in main.py
"""

from __future__ import annotations

import datetime
import json
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app import audit, auth, whatsapp

router = APIRouter(prefix="/api/connect", tags=["smc-connect"], dependencies=[Depends(auth.require_admin)])
webhook = APIRouter(prefix="/api/connect", tags=["smc-connect"])


def _bad(e: Exception):
    raise HTTPException(status_code=422, detail=str(e))


@router.get("/overview")
def overview():
    return {"connection": whatsapp.connection(), "stats": whatsapp.stats(), "roles": list(whatsapp.ROLES),
            "priorities": list(whatsapp.PRIORITIES)}


class ConnectIn(BaseModel):
    country_code: str = Field(max_length=6)
    number: str = Field(max_length=20)


@router.post("/connect")
def connect(body: ConnectIn, principal: dict = Depends(auth.require_admin)):
    try:
        c = whatsapp.start_connect(body.country_code, body.number)
    except ValueError as e:
        _bad(e)
    audit.record("connect.start", principal, target="whatsapp")
    return c


@router.post("/verify")
def verify(principal: dict = Depends(auth.require_admin)):
    c = whatsapp.verify()
    audit.record("connect.verify", principal, target="whatsapp", status=c["status"])
    return c


@router.post("/disconnect")
def disconnect(principal: dict = Depends(auth.require_admin)):
    audit.record("connect.disconnect", principal, target="whatsapp")
    return whatsapp.disconnect()


class RecipientIn(BaseModel):
    name: str = Field(max_length=80)
    country_code: str = Field(max_length=6)
    number: str = Field(max_length=20)
    role: str


class RecipientUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    role: str | None = None
    active: bool | None = None


@router.get("/recipients")
def recipients():
    return {"recipients": whatsapp.list_recipients(), "roles": list(whatsapp.ROLES)}


@router.post("/recipients")
def add_recipient(body: RecipientIn):
    try:
        return whatsapp.add_recipient(body.name, body.country_code, body.number, body.role)
    except ValueError as e:
        _bad(e)


@router.patch("/recipients/{rid}")
def update_recipient(rid: int, body: RecipientUpdate):
    try:
        r = whatsapp.update_recipient(rid, **body.model_dump(exclude_unset=True))
    except ValueError as e:
        _bad(e)
    if r is None:
        raise HTTPException(status_code=404, detail="Recipient not found")
    return r


@router.delete("/recipients/{rid}")
def delete_recipient(rid: int):
    whatsapp.delete_recipient(rid)
    return {"ok": True}


@router.get("/rules")
def rules():
    return {"rules": whatsapp.list_rules()}


class RuleUpdate(BaseModel):
    enabled: bool | None = None
    recipient_ids: list[int] | None = None
    priority: str | None = None
    preferences: dict | None = None
    template: str | None = Field(default=None, max_length=1000)
    reset_template: bool = False


@router.patch("/rules/{key}")
def update_rule(key: str, body: RuleUpdate):
    try:
        return whatsapp.update_rule(key, **body.model_dump(exclude_unset=True))
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown notification rule")
    except ValueError as e:
        _bad(e)


class PreviewIn(BaseModel):
    key: str
    template: str = Field(max_length=1000)


@router.post("/preview")
def preview(body: PreviewIn):
    if body.key not in whatsapp.EVENTS:
        raise HTTPException(status_code=404, detail="Unknown notification rule")
    return {"preview": whatsapp.render(body.template, whatsapp.EVENTS[body.key][4]), "sample": True}


class TestIn(BaseModel):
    recipient_id: int | None = None
    country_code: str | None = Field(default=None, max_length=6)
    number: str | None = Field(default=None, max_length=20)
    event_key: str = "daily_summary"


@router.post("/test")
def test(body: TestIn, principal: dict = Depends(auth.require_admin)):
    """Sends one real test message. The answer is the logged outcome: sent (Meta accepted it),
    failed (Meta's error) or not_sent (not connected); never an assumed success."""
    if body.event_key not in whatsapp.EVENTS:
        raise HTTPException(status_code=404, detail="Unknown notification rule")
    try:
        m = whatsapp.send_test(body.recipient_id, body.country_code, body.number, body.event_key)
    except ValueError as e:
        _bad(e)
    audit.record("connect.test", principal, target="whatsapp", status=m["status"])
    return m


@router.get("/history")
def history(q: str = Query("", max_length=100), event: str | None = None, status: str | None = None,
            start: str | None = None, end: str | None = None):
    try:
        s = datetime.datetime.combine(datetime.date.fromisoformat(start), datetime.time.min).timestamp() if start else None
        e = datetime.datetime.combine(datetime.date.fromisoformat(end), datetime.time.max).timestamp() if end else None
    except ValueError:
        raise HTTPException(status_code=422, detail="Dates must look like 2026-11-03")
    if status and status not in whatsapp.STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of: {', '.join(whatsapp.STATUSES)}")
    return {"messages": whatsapp.history(q, event or None, status or None, s, e)}


# ---- Meta webhook (public) ----------------------------------------------------------------

@webhook.get("/webhook")
def webhook_verify(request: Request):
    p = request.query_params
    token = os.environ.get("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "")
    if p.get("hub.mode") == "subscribe" and token and p.get("hub.verify_token") == token:
        return Response(p.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@webhook.post("/webhook")
async def webhook_statuses(request: Request):
    raw = await request.body()
    if not whatsapp.check_signature(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=401, detail="Bad signature")
    try:
        payload = json.loads(raw.decode() or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="Not JSON")
    return {"updated": whatsapp.apply_statuses(payload)}
