"""master_link.py

SMC's connection to the Deco Vision Master Portal (MAINDV), used only when
INTEGRATION_MODE=master. In the default "standalone" mode every check here
passes and nothing is fetched, so SMC behaves exactly as before.

What the Master Portal decides, and how SMC learns it:

  * Who may sign in. A client signs in on the Master Portal's client login;
    it sends the browser to SMC's /sso page with a launch token: a JWT signed
    with the portal's Ed25519 PRIVATE key, valid for one minute, addressed to
    "smc" and naming the license. verify_launch_token() checks it with the
    pinned PUBLIC key (MASTER_PORTAL_PUBLIC_KEY), checks the license id, and
    accepts each token id (jti) once only. No password or database credential
    is ever shared between the two applications.

  * The license itself: status, expiry, camera limit and features. A
    background thread reads GET /api/integration/entitlement every
    ENTITLEMENT_REFRESH_SECONDS with this deployment's application key and
    keeps the last answer (in memory and in the database, so a restart
    doesn't lose it). If the portal can't be reached, the last answer is used
    for ENTITLEMENT_GRACE_HOURS, then everything locks until it is reachable.

  * Features. require(feature) is a route dependency (403 when the license
    doesn't include it) and analytics_allowed() gates the background
    processing switches (analytics_settings.enabled). Turning a feature off
    never deletes anything: data, models and training history stay, and come
    back when it is turned on again.

SMC also reports its camera list (id, name, site only) back to the portal so
the administrator can see the cameras counted against the limit.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import logging
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import jwt
from fastapi import HTTPException

from . import config, db

log = logging.getLogger("master_link")

DB_PATH = config.DB_PATH

ISSUER = "deco-vision-master"
AUDIENCE = "smc"
CLOCK_LEEWAY_SECONDS = 10
MAX_TOKEN_LIFETIME_SECONDS = 300   # refuse anything issued for longer than this
HTTP_TIMEOUT_SECONDS = 10.0

# Master Portal feature keys (MAINDV license_db APP_FEATURES["smc"]).
FEATURE_LABELS = {
    "face_recognition": "Face recognition",
    "attendance": "Attendance",
    "face_training": "Face model training",
    "visitor_management": "Visitor management",
    "footfall_analytics": "Footfall analytics",
    "workforce_analytics": "Workforce analytics",
    "intrusion_detection": "Restricted areas",
}

# SMC's processing switches (analytics_settings.FEATURES) -> the license
# feature each one needs. Switches not listed (alerts, live boxes, expression)
# are part of every SMC license.
ANALYTICS_LICENSE_FEATURE = {
    "face_recognition": "face_recognition",
    "appearance_handoff": "face_recognition",
    "desk_analytics": "workforce_analytics",
    "footfall": "footfall_analytics",
    "visitors": "visitor_management",
    "staff_count": "attendance",
    "intrusion": "intrusion_detection",
}

_lock = threading.Lock()
_ent: dict | None = None        # last entitlement read from the portal
_fetched_at = 0.0               # when it was read (time.time())
_last_error: str | None = None
_failing_since: float | None = None     # first failed refresh of the current outage
_last_failure_log = 0.0

# The portal's pass opens SMC's login form, it never logs anyone in: it is
# exchanged for a login ticket, and in portal mode the login form needs that
# ticket AND a correct SMC email and password.
LOGIN_TICKET_SECONDS = 300
LOGIN_TICKET_MAX_ATTEMPTS = 5
# While the portal is unreachable, log once, then at most this often.
FAILURE_LOG_INTERVAL_SECONDS = 1800
_last_reported: str | None = None
_thread_started = False


class LaunchError(Exception):
    """A launch token that must not sign anyone in (the message is for logs)."""


def enabled() -> bool:
    return config.INTEGRATION_MODE == "master"


def _conn():
    return db.connect(DB_PATH, row_factory=sqlite3.Row)


def init_db() -> None:
    global _ent, _fetched_at
    with _conn() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS master_entitlement ("
            " id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL, fetched_at REAL NOT NULL)"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS sso_used_tokens (jti TEXT PRIMARY KEY, expires_at REAL NOT NULL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sso_login_tickets ("
            " ticket_hash TEXT PRIMARY KEY, license_id TEXT NOT NULL, expires_at REAL NOT NULL,"
            " attempts INTEGER NOT NULL DEFAULT 0)"
        )
        row = conn.execute("SELECT payload, fetched_at FROM master_entitlement WHERE id = 1").fetchone()
    if row is not None:
        saved = json.loads(row["payload"])
        # Only trust a saved answer for the license this deployment is set to.
        if saved.get("license_id") == config.MASTER_LICENSE_ID:
            with _lock:
                _ent, _fetched_at = saved, row["fetched_at"]


# --- Launch token ---------------------------------------------------------------

def _public_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    raw = config.MASTER_PORTAL_PUBLIC_KEY
    if not raw:
        raise LaunchError("MASTER_PORTAL_PUBLIC_KEY is not set")
    try:
        if raw.startswith("-----BEGIN"):
            key = serialization.load_pem_public_key(raw.encode())
        else:
            key = Ed25519PublicKey.from_public_bytes(base64.b64decode(raw))
    except ValueError as e:
        raise LaunchError(f"MASTER_PORTAL_PUBLIC_KEY is not a valid Ed25519 public key: {e}") from e
    if not isinstance(key, Ed25519PublicKey):
        raise LaunchError("MASTER_PORTAL_PUBLIC_KEY must be an Ed25519 key")
    return key


def verify_launch_token(token: str) -> dict:
    """The token's claims if it may sign someone in, else LaunchError. Marks
    the token as used, so a second attempt with the same token fails."""
    if not enabled():
        raise LaunchError("Master Portal sign-in is not enabled (INTEGRATION_MODE)")
    if not token or not isinstance(token, str) or len(token) > 4096:
        raise LaunchError("missing or oversized token")
    try:
        claims = jwt.decode(
            token, _public_key(), algorithms=["EdDSA"], audience=AUDIENCE, issuer=ISSUER,
            leeway=CLOCK_LEEWAY_SECONDS, options={"require": ["exp", "iat", "sub", "jti", "aud", "iss"]},
        )
    except jwt.InvalidTokenError as e:
        raise LaunchError(f"invalid token: {e}") from e
    if claims["sub"] != config.MASTER_LICENSE_ID:
        raise LaunchError("token is for a different license")
    if claims["exp"] - claims["iat"] > MAX_TOKEN_LIFETIME_SECONDS:
        raise LaunchError("token lifetime is too long")
    _consume(str(claims["jti"]), float(claims["exp"]))
    return claims


def _consume(jti: str, expires_at: float) -> None:
    now = time.time()
    with _conn() as conn:
        conn.execute("DELETE FROM sso_used_tokens WHERE expires_at < ?", (now - 3600,))
        try:
            conn.execute("INSERT INTO sso_used_tokens (jti, expires_at) VALUES (?, ?)", (jti, expires_at))
        except sqlite3.IntegrityError as e:
            raise LaunchError("token was already used") from e


# --- Login tickets ----------------------------------------------------------------

def _ticket_hash(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


def create_login_ticket(claims: dict) -> str:
    """After a verified pass: a random ticket the login form must send with the
    SMC email and password. Stored only as a hash; LOGIN_TICKET_SECONDS long."""
    ticket = secrets.token_urlsafe(32)
    now = time.time()
    with _conn() as conn:
        conn.execute("DELETE FROM sso_login_tickets WHERE expires_at < ?", (now,))
        conn.execute("INSERT INTO sso_login_tickets (ticket_hash, license_id, expires_at) VALUES (?, ?, ?)",
                     (_ticket_hash(ticket), claims["sub"], now + LOGIN_TICKET_SECONDS))
    return ticket


def login_ticket_valid(ticket: str | None) -> bool:
    """Is this a live ticket for this deployment's license with attempts left?"""
    if not ticket or not isinstance(ticket, str) or len(ticket) > 128:
        return False
    with _conn() as conn:
        row = conn.execute("SELECT license_id, expires_at, attempts FROM sso_login_tickets WHERE ticket_hash = ?",
                           (_ticket_hash(ticket),)).fetchone()
    return bool(row and row["license_id"] == config.MASTER_LICENSE_ID and row["expires_at"] >= time.time()
                and row["attempts"] < LOGIN_TICKET_MAX_ATTEMPTS)


def login_ticket_failed(ticket: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE sso_login_tickets SET attempts = attempts + 1 WHERE ticket_hash = ?", (_ticket_hash(ticket),))


def use_login_ticket(ticket: str) -> None:
    """A successful login spends the ticket."""
    with _conn() as conn:
        conn.execute("DELETE FROM sso_login_tickets WHERE ticket_hash = ?", (_ticket_hash(ticket),))


# --- Entitlement ------------------------------------------------------------------

def _call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{config.MASTER_PORTAL_URL}{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {config.MASTER_APP_API_KEY}", "Content-Type": "application/json",
                 "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as res:  # noqa: S310 - configured https URL
        return json.loads(res.read().decode())


def _store(ent: dict, fetched_at: float) -> None:
    global _ent, _fetched_at, _last_error
    with _lock:
        _ent, _fetched_at, _last_error = ent, fetched_at, None
    with _conn() as conn:
        conn.execute(
            "INSERT INTO master_entitlement (id, payload, fetched_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, fetched_at = excluded.fetched_at",
            (json.dumps(ent), fetched_at),
        )


def _refresh_failed(message: str) -> None:
    """Records a failed refresh. Logged when an outage starts, then at most every
    FAILURE_LOG_INTERVAL_SECONDS with how long it has lasted and when SMC locks."""
    global _last_error, _failing_since, _last_failure_log
    now = time.time()
    _last_error = message
    if _failing_since is None:
        _failing_since = now
    if now - _last_failure_log >= FAILURE_LOG_INTERVAL_SECONDS:
        _last_failure_log = now
        grace_until = state().get("grace_until")
        until = (time.strftime("%Y-%m-%d %H:%M", time.localtime(grace_until))
                 if grace_until and grace_until > now else "now (no valid license on record)")
        log.warning("license check failing for %.0f min: %s. SMC keeps working until %s, then locks.",
                    (now - _failing_since) / 60, message, until)


def refresh() -> dict | None:
    """Reads the license from the portal now. Returns it, or None (and keeps
    the previous answer) if the portal couldn't be reached or refused."""
    global _ent, _failing_since, _last_failure_log
    if not enabled():
        return None
    try:
        ent = _call("GET", "/api/integration/entitlement")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # The key was replaced or the license deleted: that is an answer,
            # not an outage, so the grace period must not keep access open.
            with _lock:
                _ent = None
        _refresh_failed(f"Master Portal answered {e.code}")
        return None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        _refresh_failed(f"Master Portal unreachable: {e}")
        return None
    if ent.get("license_id") != config.MASTER_LICENSE_ID:
        _refresh_failed("Master Portal returned a different license than MASTER_LICENSE_ID")
        return None
    if _failing_since is not None:
        log.info("license check working again after %.0f min", (time.time() - _failing_since) / 60)
    _failing_since, _last_failure_log = None, 0.0
    _store(ent, time.time())
    return ent


def report_cameras(force: bool = False) -> None:
    """Sends the camera list (id, name, site; never a stream URL) when it changed."""
    global _last_reported
    if not enabled():
        return
    from . import camera_db

    cams = [{"id": c["id"], "name": c.get("name") or "", "site": c.get("site") or ""} for c in camera_db.list_cameras()]
    payload = json.dumps(cams, sort_keys=True)
    if payload == _last_reported and not force:
        return
    try:
        _call("POST", "/api/integration/cameras", {"cameras": cams})
        _last_reported = payload
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("camera report failed: %s", e)


def entitlement() -> dict | None:
    """The license as last read, or None if there is none or it is older
    than the grace period (nothing is then allowed)."""
    with _lock:
        ent, fetched_at = _ent, _fetched_at
    if ent is None or time.time() - fetched_at > config.ENTITLEMENT_GRACE_HOURS * 3600:
        return None
    return ent


def license_problem() -> str | None:
    """Why the license doesn't allow access right now, or None if it does."""
    if not enabled():
        return None
    ent = entitlement()
    if ent is None:
        return "Your SMC license could not be verified with Deco Vision — try again shortly or contact your administrator"
    status = ent.get("status")
    if status == "active" and not ent.get("non_expiring") and ent.get("expires_at") and ent["expires_at"] <= time.time():
        status = "expired"
    if status != "active":
        return f"Your SMC license is {status} — contact your administrator"
    return None


def feature_allowed(feature: str | None) -> bool:
    if not enabled() or not feature:
        return True
    ent = entitlement()
    return bool(ent) and feature in (ent.get("features") or [])


def analytics_allowed(analytics_key: str) -> bool:
    """For analytics_settings.enabled(): may this processing switch run? Never
    while the license is suspended, expired or unverified (the pipelines check
    this per frame, so they pause at once and resume when it is active again;
    nothing is deleted)."""
    if license_problem() is not None:
        return False
    return feature_allowed(ANALYTICS_LICENSE_FEATURE.get(analytics_key))


def state() -> dict:
    """For monitoring (/ready) and the admin banner: standalone | ok | grace |
    locked. `grace` = the portal can't be reached right now but the last answer
    is recent enough; SMC keeps working until grace_until."""
    if not enabled():
        return {"state": "standalone"}
    with _lock:
        fetched_at = _fetched_at
    grace_until = fetched_at + config.ENTITLEMENT_GRACE_HOURS * 3600 if fetched_at else None
    problem = license_problem()
    if problem:
        st = "locked"
    elif _failing_since is not None:
        st = "grace"
    else:
        st = "ok"
    return {"state": st, "checked_at": fetched_at or None, "grace_until": grace_until,
            "failing_since": _failing_since, "problem": problem, "last_error": _last_error}


def require(feature: str):
    """Route dependency: 403 unless the license includes `feature`."""
    def dep() -> None:
        if not feature_allowed(feature):
            raise HTTPException(
                status_code=403,
                detail=f"{FEATURE_LABELS.get(feature, feature)} is not included in your SMC license",
            )
    return dep


def max_cameras() -> int | None:
    """The camera limit, None in standalone mode, 0 if the license is unknown."""
    if not enabled():
        return None
    ent = entitlement()
    return int(ent.get("max_cameras") or 0) if ent else 0


def summary() -> dict:
    """What the frontend shows and gates on."""
    if not enabled():
        return {"mode": "standalone"}
    ent = entitlement()
    st = state()
    with _lock:
        fetched_at = _fetched_at
    return {
        "mode": "master",
        "login_url": config.MASTER_PORTAL_LOGIN_URL,
        "active": license_problem() is None,
        "problem": license_problem(),
        "status": ent.get("status") if ent else None,
        "features": list(ent.get("features") or []) if ent else [],
        "max_cameras": max_cameras(),
        "expires_at": ent.get("expires_at") if ent else None,
        "non_expiring": ent.get("non_expiring") if ent else None,
        "company_name": ent.get("company_name") if ent else "",
        "checked_at": fetched_at or None,
        "last_error": _last_error,
        "state": st["state"],
        "grace_until": st["grace_until"],
    }


def _loop() -> None:
    while True:
        try:
            if refresh() is not None:
                report_cameras()
        except Exception:  # never let the refresher die
            log.exception("entitlement refresh crashed")
        time.sleep(max(10.0, config.ENTITLEMENT_REFRESH_SECONDS))


def ensure_started() -> None:
    """Starts the background refresher (idempotent; no-op in standalone mode)."""
    global _thread_started
    if not enabled():
        return
    with _lock:
        if _thread_started:
            return
        _thread_started = True
    threading.Thread(target=_loop, name="master-link", daemon=True).start()
