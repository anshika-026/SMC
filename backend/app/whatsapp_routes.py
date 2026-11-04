"""
whatsapp_routes.py

SMC Connect (WhatsApp Business) API, see whatsapp.py. Everything is for a signed-in operator
except the Meta webhook, which Meta calls and which is checked by its signature instead. No answer
ever contains a Meta token or the app secret.

  GET    /api/connect/overview                 connection, setup (Embedded Signup / manual), stats
  POST   /api/connect/embedded-signup          {code, waba_id, phone_number_id, pin?}: finish Meta Embedded Signup
  POST   /api/connect/connect                  {country_code, number}: manual setup step 1 (pending)
  POST   /api/connect/verify                   ask Meta; connected only if Meta confirms
  POST   /api/connect/disconnect
  GET    /api/connect/recipients               list / POST add / PATCH {id} / DELETE {id}
  POST   /api/connect/recipients/{id}/opt-in   {method, confirmed}: record the recipient's consent
  POST   /api/connect/recipients/{id}/opt-out
  GET    /api/connect/templates                Meta message templates (POST /templates/sync re-reads Meta)
  GET    /api/connect/rules                    rules with text, linked Meta template and previews
  PATCH  /api/connect/rules/{key}              enabled, recipients, priority, preferences, text, Meta template
  POST   /api/connect/preview                  {key, template}: preview with sample values
  POST   /api/connect/test                     {recipient_id, event_key}: one real test message
  GET    /api/connect/history?q&event&status&start&end
  GET    /api/connect/webhook                  Meta's verification handshake (public)
  POST   /api/connect/webhook                  Meta's callbacks (public, signed)

Mount with: app.include_router(whatsapp_routes.router) and whatsapp_routes.webhook in main.py
"""

from __future__ import annotations

import datetime
import hmac
import json
import os

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app import audit, auth, whatsapp

router = APIRouter(prefix="/api/connect", tags=["smc-connect"], dependencies=[Depends(auth.require_admin)])
webhook = APIRouter(prefix="/api/connect", tags=["smc-connect"])


def _bad(e: Exception):
    raise HTTPException(status_code=422, detail=str(e))


def _who(principal: dict) -> str:
    return principal.get("email") or principal.get("sso_username") or principal.get("sso_name") or "admin"


@router.get("/overview")
def overview():
    return {"connection": whatsapp.connection(), "embedded_signup": whatsapp.embedded_signup_config(), "stats": whatsapp.stats(),
            "roles": list(whatsapp.ROLES), "priorities": list(whatsapp.PRIORITIES),
            "opt_in_methods": {k: v for k, v in whatsapp.OPT_IN_METHODS.items() if k != "whatsapp_reply"}}


class EmbeddedSignupIn(BaseModel):
    code: str = Field(max_length=2048)
    waba_id: str = Field(max_length=32)
    phone_number_id: str = Field(max_length=32)
    pin: str | None = Field(default=None, max_length=6)


@router.post("/embedded-signup")
def embedded_signup(body: EmbeddedSignupIn, principal: dict = Depends(auth.require_admin)):
    try:
        c = whatsapp.complete_embedded_signup(body.code, body.waba_id, body.phone_number_id, body.pin)
    except whatsapp.NotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))
    except ValueError as e:
        _bad(e)
    audit.record("connect.embedded_signup", principal, target="whatsapp", status=c["status"])
    return c


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


class OptInIn(BaseModel):
    method: str
    confirmed: bool = False


@router.post("/recipients/{rid}/opt-in")
def opt_in(rid: int, body: OptInIn, principal: dict = Depends(auth.require_admin)):
    if not body.confirmed:
        raise HTTPException(status_code=422, detail="Confirm that this person agreed to receive WhatsApp notifications")
    try:
        r = whatsapp.record_opt_in(rid, body.method, _who(principal))
    except ValueError as e:
        _bad(e)
    if r is None:
        raise HTTPException(status_code=404, detail="Recipient not found")
    audit.record("connect.opt_in", principal, target=f"wa_recipient:{rid}", method=body.method)
    return r


@router.post("/recipients/{rid}/opt-out")
def opt_out(rid: int, principal: dict = Depends(auth.require_admin)):
    r = whatsapp.record_opt_out(rid, _who(principal))
    if r is None:
        raise HTTPException(status_code=404, detail="Recipient not found")
    audit.record("connect.opt_out", principal, target=f"wa_recipient:{rid}")
    return r


@router.delete("/recipients/{rid}")
def delete_recipient(rid: int):
    whatsapp.delete_recipient(rid)
    return {"ok": True}


@router.get("/templates")
def templates():
    return {"templates": whatsapp.list_templates()}


@router.post("/templates/sync")
def sync_templates(principal: dict = Depends(auth.require_admin)):
    try:
        t = whatsapp.sync_templates()
    except whatsapp.NotConfigured as e:
        raise HTTPException(status_code=409, detail=str(e))
    except whatsapp.MetaError as e:
        raise HTTPException(status_code=502, detail=f"Meta: {e}")
    audit.record("connect.templates_sync", principal, target="whatsapp", count=len(t))
    return {"templates": t}


@router.get("/rules")
def rules():
    return {"rules": whatsapp.list_rules()}


class MetaTemplateIn(BaseModel):
    name: str = Field(max_length=512)
    language: str = Field(max_length=16)
    params: list[str] = Field(default_factory=list, max_length=20)


class RuleUpdate(BaseModel):
    enabled: bool | None = None
    recipient_ids: list[int] | None = None
    priority: str | None = None
    preferences: dict | None = None
    template: str | None = Field(default=None, max_length=1000)
    reset_template: bool = False
    meta_template: MetaTemplateIn | None = None
    clear_meta_template: bool = False


@router.patch("/rules/{key}")
def update_rule(key: str, body: RuleUpdate):
    data = body.model_dump(exclude_unset=True)
    try:
        return whatsapp.update_rule(key, **data)
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
    recipient_id: int
    event_key: str = "daily_summary"


@router.post("/test")
def test(body: TestIn, principal: dict = Depends(auth.require_admin)):
    """Sends one real test message to a recipient. The answer is the logged outcome: sent (Meta
    accepted it), failed (Meta's error) or not_sent (the reason); never an assumed success."""
    if body.event_key not in whatsapp.EVENTS:
        raise HTTPException(status_code=404, detail="Unknown notification rule")
    try:
        m = whatsapp.send_test(body.recipient_id, body.event_key)
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
    if p.get("hub.mode") == "subscribe" and token and hmac.compare_digest(p.get("hub.verify_token", ""), token):
        return Response(p.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@webhook.post("/webhook")
async def webhook_events(request: Request):
    raw = await request.body()
    if not whatsapp.check_signature(raw, request.headers.get("x-hub-signature-256")):
        raise HTTPException(status_code=401, detail="Bad signature")
    try:
        payload = json.loads(raw.decode() or "{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="Not JSON")
    return whatsapp.handle_webhook(payload)
