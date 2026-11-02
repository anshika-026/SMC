"""
sso_routes.py

Sign-in from the Deco Vision Master Portal (see master_link.py):

  GET  /api/auth/sso/config   public: is SMC connected, and where clients sign in
  POST /api/auth/sso          {token}: exchange a launch token for an SMC session

The frontend's /sso page reads the token from the URL fragment (#token=...),
which browsers never send to a server, and posts it here once.

Mount with: app.include_router(sso_routes.router) in main.py
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import audit, auth, config, master_link, ratelimit

log = logging.getLogger("sso")

router = APIRouter(prefix="/api/auth/sso", tags=["sso"])

# Same throttle shape as the login form: a burst of bad tokens from one
# address is refused before any signature work.
_guard = ratelimit.LoginGuard("sso", per_ip=30, per_user=30, window=300)


@router.get("/config")
def sso_config():
    if not master_link.enabled():
        return {"mode": "standalone"}
    return {"mode": "master", "login_url": config.MASTER_PORTAL_LOGIN_URL}


class SsoIn(BaseModel):
    token: str = Field(min_length=1, max_length=4096)


@router.post("")
def sso_exchange(payload: SsoIn, request: Request):
    if not master_link.enabled():
        raise HTTPException(status_code=404, detail="Not found")
    ip = ratelimit.client_ip(request)
    _guard.check(ip, "sso")
    try:
        claims = master_link.verify_launch_token(payload.token)
    except master_link.LaunchError as e:
        _guard.failed(ip, "sso")
        log.warning("refused a Master Portal sign-in from %s: %s", ip, e)
        raise HTTPException(status_code=401, detail="This sign-in link is invalid or has expired. Please sign in again.")
    _guard.succeeded(ip, "sso")
    # Read the license now, not from the cache: a suspension made a moment ago
    # must not slip through, and a reactivation must not be refused because
    # the cached copy still says "suspended". If the portal can't be reached,
    # the cached copy (within its grace period) decides.
    master_link.refresh()
    problem = master_link.license_problem()
    if problem:
        raise HTTPException(status_code=403, detail=problem)
    token = auth.create_sso_session(claims)
    name = claims.get("name") or claims.get("company") or claims.get("username")
    audit.record("sso.login", {"email": claims.get("username")}, ip=ip, license=claims["sub"])
    return {"token": token, "name": name, "username": claims.get("username"), "sso": True,
            "license": master_link.summary()}
