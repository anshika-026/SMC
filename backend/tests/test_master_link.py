"""SMC connected to the Deco Vision Master Portal (master_link.py, sso_routes.py).

A fake portal stands in for MAINDV: tests sign launch tokens with their own
Ed25519 key and decide what GET /api/integration/entitlement answers.

Sign-in in portal mode: the portal's one-minute pass is exchanged for a login
ticket (POST /api/auth/sso), and the normal login form then needs that ticket
AND an SMC email and password. The pass alone never logs anyone in."""

import base64
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import auth, camera_db, config, master_link
from tests.conftest import ADMIN_EMAIL, ADMIN_PASSWORD, bearer

LICENSE_ID = "lic-smc-0001"
ALL_SMC_FEATURES = ["face_recognition", "attendance", "face_training", "visitor_management",
                    "footfall_analytics", "workforce_analytics", "intrusion_detection"]


class FakePortal:
    def __init__(self):
        self.ent = {"license_id": LICENSE_ID, "app": "smc", "company_name": "SMC", "label": "SMC",
                    "status": "active", "expires_at": time.time() + 30 * 86400, "non_expiring": False,
                    "max_cameras": 2, "features": list(ALL_SMC_FEATURES)}
        self.down = False
        self.reported = None

    def call(self, method, path, body=None):
        if self.down:
            raise master_link.urllib.error.URLError("connection refused")
        if path == "/api/integration/entitlement":
            return dict(self.ent)
        if path == "/api/integration/cameras":
            self.reported = body["cameras"]
            return {"recorded": len(body["cameras"])}
        raise AssertionError(path)


@pytest.fixture
def tables(isolated_db, monkeypatch):
    """Tables of the feature modules these tests call (not created by conftest)."""
    from app import attendance, intrusion, presence, visitor_registry, workforce

    for mod in (attendance, workforce, presence, visitor_registry, intrusion):
        monkeypatch.setattr(mod, "DB_PATH", isolated_db)
        mod.init_db()


@pytest.fixture
def lenient(api):
    """Same app, but a server error comes back as a 500 instead of raising:
    for checks that only care whether the license gate let a request through."""
    from fastapi.testclient import TestClient

    from app import main

    return TestClient(main.app, raise_server_exceptions=False)


@pytest.fixture
def portal(api, tables, monkeypatch):
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    monkeypatch.setattr(config, "INTEGRATION_MODE", "master")
    monkeypatch.setattr(config, "MASTER_LICENSE_ID", LICENSE_ID)
    monkeypatch.setattr(config, "MASTER_PORTAL_PUBLIC_KEY", base64.b64encode(raw).decode())
    monkeypatch.setattr(config, "MASTER_PORTAL_LOGIN_URL", "https://vision.example.com/client/smc/login")
    fake = FakePortal()
    monkeypatch.setattr(master_link, "_call", fake.call)
    monkeypatch.setattr(master_link, "_ent", None)
    monkeypatch.setattr(master_link, "_fetched_at", 0.0)
    monkeypatch.setattr(master_link, "_last_reported", None)
    monkeypatch.setattr(master_link, "_failing_since", None)
    monkeypatch.setattr(master_link, "_last_failure_log", 0.0)
    fake.key = key
    master_link.refresh()
    auth.create_admin_user(ADMIN_EMAIL, ADMIN_PASSWORD, "Admin")   # SMC's own account
    return fake


def launch_token(portal, **over):
    now = int(time.time())
    claims = {"iss": "deco-vision-master", "aud": "smc", "sub": LICENSE_ID, "jti": f"j-{time.time_ns()}",
              "iat": now, "nbf": now, "exp": now + 60, "username": "smc_client", "name": "", "role": "client_admin",
              "company": "SMC", "via": "client", "launched_by": ""}
    claims.update(over)
    key = over.pop("_key", None) or portal.key
    claims.pop("_key", None)
    return jwt.encode(claims, key, algorithm="EdDSA")


def sign_in(api, token):
    """Step 1: exchange a pass for a login ticket."""
    return api.post("/api/auth/sso", json={"token": token})


def login(api, ticket=None, email=ADMIN_EMAIL, password=ADMIN_PASSWORD):
    """Step 2: the SMC login form."""
    return api.post("/api/auth/login", json={"email": email, "password": password, "portal_ticket": ticket})


def portal_login(api, portal):
    """The whole journey: pass -> ticket -> SMC email and password -> session token."""
    r = sign_in(api, launch_token(portal))
    assert r.status_code == 200, r.text
    r = login(api, r.json()["ticket"])
    assert r.status_code == 200, r.text
    return r.json()["token"]


# --- standalone mode is untouched ---------------------------------------------------

def test_standalone_mode_has_no_sso_and_no_restrictions(api, tables, admin_token):
    assert api.get("/api/auth/sso/config").json() == {"mode": "standalone"}
    assert api.post("/api/auth/sso", json={"token": "x"}).status_code == 404
    assert master_link.feature_allowed("face_training") and master_link.max_cameras() is None
    assert master_link.analytics_allowed("face_recognition") and master_link.state() == {"state": "standalone"}
    me = api.get("/api/auth/me", headers=bearer(admin_token)).json()
    assert me["license"] == {"mode": "standalone"} and me["sso"] is False
    assert api.get("/api/visitor-records/settings", headers=bearer(admin_token)).status_code == 200


def test_standalone_login_needs_no_ticket(api, tables, admin_token):
    r = login(api)          # admin_token already logged in once; a second login works the same
    assert r.status_code == 200 and r.json()["token"]


# --- sign-in: pass, then the SMC login ----------------------------------------------------

def test_a_pass_gives_a_login_ticket_never_a_session(api, portal):
    r = sign_in(api, launch_token(portal))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticket"] and body["expires_in"] == master_link.LOGIN_TICKET_SECONDS
    assert "token" not in body and "session_token" not in body and body["license"]["active"] is True
    # the ticket is not a session
    assert api.get("/api/auth/me", headers=bearer(body["ticket"])).status_code == 401


def test_pass_then_existing_smc_password_opens_smc(api, lenient, portal):
    h = bearer(portal_login(api, portal))
    me = api.get("/api/auth/me", headers=h).json()
    assert me["email"] == ADMIN_EMAIL and me["sso"] is False      # the SMC account, not the portal's
    assert set(me["license"]["features"]) == set(ALL_SMC_FEATURES)
    # the existing SMC pages work with this session
    assert api.get("/api/cameras", headers=h).status_code == 200
    assert lenient.get("/api/dashboard/summary", headers=h).status_code not in (401, 403)
    assert api.get("/api/visitor-records/settings", headers=h).status_code == 200


def test_portal_mode_login_without_a_ticket_is_refused(api, portal):
    r = login(api, None)
    assert r.status_code == 401 and "Deco Vision" in r.json()["detail"]
    assert login(api, "made-up-ticket").status_code == 401


def test_a_ticket_with_the_wrong_password_does_not_log_in_and_runs_out(api, portal):
    ticket = sign_in(api, launch_token(portal)).json()["ticket"]
    for _ in range(master_link.LOGIN_TICKET_MAX_ATTEMPTS):
        r = login(api, ticket, password="wrong-password")
        assert r.status_code in (401, 429)
    # attempts used up: even the right password is refused with this ticket
    assert login(api, ticket).status_code in (401, 429)


def test_a_ticket_works_for_one_login_only(api, portal):
    ticket = sign_in(api, launch_token(portal)).json()["ticket"]
    assert login(api, ticket).status_code == 200
    assert login(api, ticket).status_code == 401


def test_an_expired_ticket_is_refused(api, portal, monkeypatch):
    ticket = sign_in(api, launch_token(portal)).json()["ticket"]
    real = time.time
    monkeypatch.setattr(master_link.time, "time", lambda: real() + master_link.LOGIN_TICKET_SECONDS + 5)
    assert not master_link.login_ticket_valid(ticket)


def test_the_pass_role_never_makes_anyone_admin(api, portal):
    """Whatever the pass claims, SMC's own account decides who signs in."""
    ticket = sign_in(api, launch_token(portal, role="client_admin", username="someone")).json()["ticket"]
    assert login(api, ticket, email="someone@example.com", password="anything").status_code == 401


def test_config_tells_the_frontend_where_to_sign_in(api, portal):
    assert api.get("/api/auth/sso/config").json() == {"mode": "master", "login_url": config.MASTER_PORTAL_LOGIN_URL}


@pytest.mark.parametrize("bad", [
    {"exp": int(time.time()) - 120, "iat": int(time.time()) - 180},   # expired
    {"nbf": int(time.time()) + 600},                                     # not valid yet
    {"aud": "deco_vision"},                                              # for another application
    {"iss": "someone-else"},                                             # not the Master Portal
    {"sub": "lic-welmont"},                                              # another client's license
    {"exp": int(time.time()) + 3600},                                    # far too long-lived
])
def test_invalid_launch_tokens_are_refused(api, portal, bad):
    r = sign_in(api, launch_token(portal, **bad))
    assert r.status_code == 401
    assert "ticket" not in r.json()


def test_token_signed_with_another_key_is_refused(api, portal):
    assert sign_in(api, launch_token(portal, _key=Ed25519PrivateKey.generate())).status_code == 401


def test_garbage_and_unsigned_tokens_are_refused(api, portal):
    assert sign_in(api, "not-a-jwt").status_code == 401
    unsigned = jwt.encode({"iss": "deco-vision-master", "aud": "smc", "sub": LICENSE_ID, "jti": "x",
                           "iat": int(time.time()), "exp": int(time.time()) + 60}, key=None, algorithm="none")
    assert sign_in(api, unsigned).status_code == 401


def test_a_launch_token_works_only_once(api, portal):
    token = launch_token(portal)
    assert sign_in(api, token).status_code == 200
    assert sign_in(api, token).status_code == 401


@pytest.mark.parametrize("status", ["suspended", "inactive", "expired"])
def test_suspended_or_expired_license_cannot_sign_in(api, portal, status):
    portal.ent["status"] = status
    master_link.refresh()
    r = sign_in(api, launch_token(portal))
    assert r.status_code == 403 and status in r.json()["detail"]


def test_sign_in_reads_the_license_fresh_not_from_the_cache(api, portal):
    """Reactivated a moment after SMC cached "suspended": sign-in must work,
    and suspended a moment after it cached "active": sign-in must not."""
    portal.ent["status"] = "suspended"
    master_link.refresh()
    portal.ent["status"] = "active"          # reactivated; SMC's copy still says suspended
    assert sign_in(api, launch_token(portal)).status_code == 200
    portal.ent["status"] = "suspended"       # suspended; SMC's copy still says active
    assert sign_in(api, launch_token(portal)).status_code == 403


def test_sign_in_uses_the_cached_license_when_the_portal_is_down(api, portal):
    portal.down = True
    assert sign_in(api, launch_token(portal)).status_code == 200


def test_expiry_date_passing_locks_even_if_status_says_active(api, portal):
    portal.ent["expires_at"] = time.time() - 1
    master_link.refresh()
    assert sign_in(api, launch_token(portal)).status_code == 403


# --- a suspended license shuts out every admin, local accounts included ------------------

def test_suspended_license_refuses_the_login_form_even_with_a_ticket(api, portal):
    ticket = sign_in(api, launch_token(portal)).json()["ticket"]
    portal.ent["status"] = "suspended"
    master_link.refresh()
    r = login(api, ticket)
    assert r.status_code == 403 and "suspended" in r.json()["detail"]


def test_suspension_shuts_out_open_sessions_and_reactivation_lets_them_back(api, portal):
    h = bearer(portal_login(api, portal))
    assert api.get("/api/cameras", headers=h).status_code == 200
    portal.ent["status"] = "suspended"
    master_link.refresh()
    r = api.get("/api/cameras", headers=h)
    assert r.status_code == 401 and "suspended" in r.json()["detail"]
    portal.ent["status"] = "active"
    master_link.refresh()
    assert api.get("/api/cameras", headers=h).status_code == 200
    assert portal_login(api, portal)                       # and a new sign-in works


def test_logout_ends_the_session(api, portal):
    token = portal_login(api, portal)
    assert api.post("/api/auth/logout", headers=bearer(token)).status_code == 200
    assert api.get("/api/auth/me", headers=bearer(token)).status_code == 401


def test_session_for_another_license_is_refused(api, portal, monkeypatch):
    token = portal_login(api, portal)
    monkeypatch.setattr(config, "MASTER_LICENSE_ID", "lic-other")
    monkeypatch.setattr(master_link, "_ent", None)          # the cached answer is for the old license
    assert api.get("/api/cameras", headers=bearer(token)).status_code == 401


def test_session_row_belongs_to_the_smc_account(api, portal):
    s = auth.get_session(portal_login(api, portal))
    assert s["admin_user_id"] is not None and not s.get("sso_license_id")


# --- AI processing stops with the license -----------------------------------------------

@pytest.mark.parametrize("status", ["suspended", "inactive", "expired"])
def test_processing_stops_while_the_license_is_not_active_and_resumes(api, portal, status):
    from app import analytics_settings

    keys = ("face_recognition", "visitors", "footfall", "intrusion", "alerts", "live_overlay", "expression")
    assert all(analytics_settings.enabled(k) for k in ("face_recognition", "visitors", "alerts"))
    portal.ent["status"] = status
    master_link.refresh()
    assert not any(analytics_settings.enabled(k) for k in keys)       # every switch, licensed or not
    snap = analytics_settings.snapshot()
    assert snap["face_recognition"]["on"] is True                       # the switches themselves are kept
    portal.ent["status"] = "active"
    master_link.refresh()
    assert analytics_settings.enabled("face_recognition") and analytics_settings.enabled("alerts")


def test_processing_stops_when_the_grace_period_runs_out(api, portal, monkeypatch):
    from app import analytics_settings

    portal.down = True
    master_link.refresh()
    assert analytics_settings.enabled("face_recognition")             # within the grace period
    monkeypatch.setattr(master_link, "_fetched_at", time.time() - (config.ENTITLEMENT_GRACE_HOURS * 3600 + 5))
    assert not analytics_settings.enabled("face_recognition")


# --- portal outages -------------------------------------------------------------------

def test_portal_outage_keeps_the_last_license_within_the_grace_period(api, portal, monkeypatch):
    portal.down = True
    assert master_link.refresh() is None
    assert master_link.license_problem() is None and master_link.feature_allowed("attendance")
    st = master_link.state()
    assert st["state"] == "grace" and st["grace_until"] > time.time()
    # once the last good answer is older than the grace period, everything locks
    monkeypatch.setattr(master_link, "_fetched_at", time.time() - (config.ENTITLEMENT_GRACE_HOURS * 3600 + 5))
    assert master_link.license_problem() is not None
    assert not master_link.feature_allowed("attendance")
    assert master_link.max_cameras() == 0
    assert master_link.state()["state"] == "locked"
    # the portal answers again: back to ok
    portal.down = False
    master_link.refresh()
    assert master_link.state()["state"] == "ok"


def test_an_outage_is_logged_once_not_every_minute(api, portal, caplog):
    portal.down = True
    with caplog.at_level("WARNING", logger="master_link"):
        for _ in range(5):
            master_link.refresh()
    warnings = [r for r in caplog.records if "license check failing" in r.getMessage()]
    assert len(warnings) == 1 and "keeps working until" in warnings[0].getMessage()


def test_ready_reports_the_license_state_for_monitoring(api, portal):
    lic = api.get("/ready").json()["license"]
    assert lic["state"] == "ok" and set(lic) == {"state", "checked_at", "grace_until"}
    portal.ent["status"] = "suspended"
    master_link.refresh()
    assert api.get("/ready").json()["license"]["state"] == "locked"


def test_rejected_application_key_locks_immediately(api, portal, monkeypatch):
    def refused(method, path, body=None):
        raise master_link.urllib.error.HTTPError(path, 401, "Invalid application key", {}, None)

    monkeypatch.setattr(master_link, "_call", refused)
    master_link.refresh()
    assert master_link.license_problem() is not None


def test_license_survives_a_restart(api, portal, monkeypatch):
    monkeypatch.setattr(master_link, "_ent", None)
    monkeypatch.setattr(master_link, "_fetched_at", 0.0)
    master_link.init_db()   # what startup does
    assert master_link.entitlement()["license_id"] == LICENSE_ID


# --- features ---------------------------------------------------------------------------

FEATURE_ROUTES = {
    "face_recognition": "/api/faces/people",
    "face_training": "/api/faces/training/stats",
    "attendance": "/api/attendance/main-hall",
    "visitor_management": "/api/visitor-records/settings",
    "footfall_analytics": "/api/footfall/history",
    "workforce_analytics": "/api/desk-zones",
    "intrusion_detection": "/api/intrusion/zones",
}


@pytest.mark.parametrize("feature", list(FEATURE_ROUTES))
def test_turning_a_feature_off_blocks_its_api_and_on_restores_it(api, lenient, portal, feature):
    h = bearer(portal_login(api, portal))
    path = FEATURE_ROUTES[feature]
    assert lenient.get(path, headers=h).status_code not in (401, 403)
    portal.ent["features"] = [f for f in ALL_SMC_FEATURES if f != feature]
    master_link.refresh()
    r = lenient.get(path, headers=h)
    assert r.status_code == 403 and "not included in your SMC license" in r.json()["detail"]
    # every other feature keeps working
    for other, other_path in FEATURE_ROUTES.items():
        if other != feature:
            assert lenient.get(other_path, headers=h).status_code not in (401, 403), other
    portal.ent["features"] = list(ALL_SMC_FEATURES)
    master_link.refresh()
    assert lenient.get(path, headers=h).status_code not in (401, 403)


def test_camera_still_is_not_blocked_with_desk_analytics(api, lenient, portal):
    """Zone editors everywhere use /cameras/{id}/frame; only desk routes need workforce analytics."""
    token = portal_login(api, portal)
    portal.ent["features"] = ["attendance"]
    master_link.refresh()
    assert lenient.get("/api/cameras/999/frame", headers=bearer(token)).status_code not in (401, 403)


def test_processing_switches_follow_the_license(api, portal):
    from app import analytics_settings

    assert analytics_settings.enabled("face_recognition") and analytics_settings.enabled("visitors")
    portal.ent["features"] = ["attendance"]
    master_link.refresh()
    assert not analytics_settings.enabled("face_recognition")
    assert not analytics_settings.enabled("visitors")
    assert not analytics_settings.enabled("desk_analytics")
    assert analytics_settings.enabled("alerts")     # not a licensed feature
    snap = analytics_settings.snapshot()
    assert snap["visitors"]["on"] is True and snap["visitors"]["licensed"] is False   # the switch itself is kept


def test_dashboard_leaves_out_unlicensed_sections(api, portal):
    from app import alerts, insights

    alerts.init_db()
    portal.ent["features"] = ["intrusion_detection"]
    master_link.refresh()
    biz = insights.business_overview()
    assert biz["workforce"] is None and biz["visitors"] is None and biz["footfall"] is None


# --- cameras ------------------------------------------------------------------------------

CAMERA = {"name": "Gate", "site": "HQ", "rtsp_url": "rtsp://10.0.0.5/stream", "protocol": "rtsp"}


def test_camera_limit_is_enforced_by_the_backend(api, portal):
    h = bearer(portal_login(api, portal))
    for i in range(2):
        assert api.post("/api/cameras", json={**CAMERA, "name": f"Cam {i}"}, headers=h).status_code == 200
    r = api.post("/api/cameras", json={**CAMERA, "name": "One too many"}, headers=h)
    assert r.status_code == 403 and "allows 2 camera" in r.json()["detail"]
    assert len(camera_db.list_cameras()) == 2
    # raising the limit in the portal allows one more
    portal.ent["max_cameras"] = 3
    master_link.refresh()
    assert api.post("/api/cameras", json={**CAMERA, "name": "Cam 3"}, headers=h).status_code == 200


def test_camera_report_sends_names_only(api, portal):
    camera_db.add_camera("Gate", "HQ", rtsp_url="rtsp://user:secret@10.0.0.5/stream")
    master_link.report_cameras(force=True)
    assert portal.reported == [{"id": portal.reported[0]["id"], "name": "Gate", "site": "HQ"}]
    assert "secret" not in str(portal.reported)


def test_no_limit_in_standalone_mode(api, admin_token):
    h = bearer(admin_token)
    for i in range(3):
        assert api.post("/api/cameras", json={**CAMERA, "name": f"Cam {i}"}, headers=h).status_code == 200


# --- review fix 1: websockets follow the license lock ------------------------------------

@pytest.fixture
def staff_feed(portal):
    from app.staff import occupancy
    from app.staff.service import service as staff_service

    cam = camera_db.add_camera("Gate", "HQ", host="10.0.0.9", purpose="Entry/Exit")
    mgr = occupancy.StaffOccupancyManager()
    staff_service.manager = mgr
    now = time.time()
    mgr._event(now, "ENTRY", camera_id=cam, employee_id="EMP-1")
    mgr._present["v-1"] = {"visit_id": "v-1", "employee_id": "EMP-1", "entry_time": now,
                           "last_seen": now, "camera_id": cam, "confidence": 0.9}
    yield cam
    staff_service.manager = None


def _ws_close_code(api, url, receive="json"):
    from starlette.websockets import WebSocketDisconnect

    with api.websocket_connect(url) as ws:
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json() if receive == "json" else ws.receive_bytes()
    return e.value.code


def _lock(portal, monkeypatch, status):
    if status == "unverifiable":
        monkeypatch.setattr(master_link, "_fetched_at", time.time() - (config.ENTITLEMENT_GRACE_HOURS * 3600 + 5))
    elif status == "expired":
        portal.ent.update(status="active", expires_at=time.time() - 1)
        master_link.refresh()
    else:
        portal.ent["status"] = status
        master_link.refresh()


@pytest.mark.parametrize("status", ["suspended", "expired", "unverifiable"])
def test_all_websockets_refuse_an_admin_while_the_license_is_locked(api, portal, staff_feed, status, monkeypatch):
    from app import main

    token = portal_login(api, portal)
    _lock(portal, monkeypatch, status)
    assert not main._authorize_camera_ws(token, staff_feed)
    assert _ws_close_code(api, f"/ws/live/{staff_feed}?token={token}", "bytes") == 4401
    assert _ws_close_code(api, f"/ws/detections/{staff_feed}?token={token}") == 4401
    assert _ws_close_code(api, f"/ws/staff?token={token}") == 4401
    assert _ws_close_code(api, f"/ws/staff/debug/{staff_feed}?token={token}") == 4403


def test_websockets_work_again_after_reactivation(api, portal, staff_feed):
    from app import main

    token = portal_login(api, portal)
    portal.ent["status"] = "suspended"
    master_link.refresh()
    assert _ws_close_code(api, f"/ws/staff?token={token}") == 4401
    portal.ent["status"] = "active"
    master_link.refresh()
    assert main._authorize_camera_ws(token, staff_feed)                            # /ws/live and /ws/detections
    assert _ws_close_code(api, f"/ws/live/99999?token={token}", "bytes") != 4401   # past auth: no such camera
    with api.websocket_connect(f"/ws/staff?token={token}") as ws:
        assert ws.receive_json()["total_persons"] == 1


def test_an_open_staff_websocket_is_closed_when_the_license_is_suspended(api, portal, staff_feed):
    from starlette.websockets import WebSocketDisconnect

    token = portal_login(api, portal)
    with api.websocket_connect(f"/ws/staff?token={token}") as ws:
        assert ws.receive_json()["total_persons"] == 1
        portal.ent["status"] = "suspended"
        master_link.refresh()
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()                              # its next re-check (every 2 s) closes it
    assert e.value.code == 4401


def test_websockets_are_unchanged_in_standalone_mode(api, tables, admin_token):
    from app import main

    cam = camera_db.add_camera("Gate", "HQ", host="10.0.0.9")
    assert main._authorize_camera_ws(admin_token, cam)


# --- review fix 2: the old pass-only session is gone -------------------------------------

def _legacy_pass_only_session():
    """A session row as the previous version of POST /api/auth/sso created it."""
    token = "legacy-" + str(time.time_ns())
    with auth.get_connection() as conn:
        conn.execute(
            "INSERT INTO sessions (token, principal_type, email, created_at, expires_at, "
            " sso_license_id, sso_username, sso_name, sso_role) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (token, auth.ADMIN, "smc_client", time.time(), time.time() + 3600, LICENSE_ID, "smc_client", "SMC",
             "client_admin"))
    return token


def test_a_legacy_pass_only_session_grants_nothing(api, portal):
    token = _legacy_pass_only_session()
    assert api.get("/api/auth/me", headers=bearer(token)).status_code == 401
    assert api.get("/api/cameras", headers=bearer(token)).status_code == 401
    with auth.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions WHERE token = ?", (token,)).fetchone()[0] == 0
    assert not hasattr(auth, "create_sso_session")


def test_startup_removes_pass_only_sessions_and_keeps_normal_ones(api, portal):
    normal = portal_login(api, portal)
    legacy = _legacy_pass_only_session()
    auth.init_db()                                        # what startup does
    with auth.get_connection() as conn:
        tokens = {r[0] for r in conn.execute("SELECT token FROM sessions")}
    assert legacy not in tokens and normal in tokens
    assert api.get("/api/auth/me", headers=bearer(normal)).json()["email"] == ADMIN_EMAIL


def test_an_old_pass_alone_still_cannot_log_in(api, portal):
    r = sign_in(api, launch_token(portal, role="client_admin"))
    assert "token" not in r.json() and "session_token" not in r.json()
    assert api.get("/api/auth/me", headers=bearer(r.json()["ticket"])).status_code == 401


# --- review fix 3: SMC's own client accounts follow the license too ----------------------

@pytest.fixture
def smc_client(portal):
    from app import license_db

    company = license_db.create_company("Branch office")
    lic = license_db.create_license(company["id"], 2, "branch", username="branch_user", password="branch-pass-123456")
    license_db.set_license_features(lic["id"], ["attendance"])
    return lic


def _client_login(api):
    return api.post("/api/licenses/client-login", json={"username": "branch_user", "password": "branch-pass-123456"})


def test_client_account_works_while_the_license_is_active(api, portal, smc_client):
    r = _client_login(api)
    assert r.status_code == 200, r.text
    assert api.get("/api/cameras", headers=bearer(r.json()["session_token"])).status_code == 200


@pytest.mark.parametrize("status", ["suspended", "expired", "unverifiable"])
def test_client_login_and_open_client_sessions_are_locked(api, portal, smc_client, status, monkeypatch):
    session = _client_login(api).json()["session_token"]
    _lock(portal, monkeypatch, status)
    r = _client_login(api)
    assert r.status_code == 403 and "license" in r.json()["detail"]
    r = api.get("/api/cameras", headers=bearer(session))
    assert r.status_code == 401 and "license" in r.json()["detail"]


def test_client_account_works_again_after_reactivation(api, portal, smc_client):
    session = _client_login(api).json()["session_token"]
    portal.ent["status"] = "suspended"
    master_link.refresh()
    assert api.get("/api/cameras", headers=bearer(session)).status_code == 401
    portal.ent["status"] = "active"
    master_link.refresh()
    assert api.get("/api/cameras", headers=bearer(session)).status_code == 200
    assert _client_login(api).status_code == 200


def test_client_accounts_are_unchanged_in_standalone_mode(api, tables):
    from app import license_db

    company = license_db.create_company("Standalone client")
    license_db.create_license(company["id"], 2, "s", username="branch_user", password="branch-pass-123456")
    assert _client_login(api).status_code == 200
