"""SMC connected to the Deco Vision Master Portal (master_link.py, sso_routes.py).

A fake portal stands in for MAINDV: tests sign launch tokens with their own
Ed25519 key and decide what GET /api/integration/entitlement answers."""

import base64
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import auth, camera_db, config, master_link
from tests.conftest import bearer

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
    from app import attendance, presence, restricted_zones, visitor_registry, workforce

    for mod in (attendance, workforce, presence, visitor_registry, restricted_zones):
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
    fake.key = key
    master_link.refresh()
    return fake


def launch_token(portal, **over):
    now = int(time.time())
    claims = {"iss": "deco-vision-master", "aud": "smc", "sub": LICENSE_ID, "jti": f"j-{time.time_ns()}",
              "iat": now, "exp": now + 60, "username": "smc_client", "name": "", "role": "client_admin",
              "company": "SMC"}
    claims.update(over)
    key = over.pop("_key", None) or portal.key
    claims.pop("_key", None)
    return jwt.encode(claims, key, algorithm="EdDSA")


def sign_in(api, token):
    return api.post("/api/auth/sso", json={"token": token})


# --- standalone mode is untouched ---------------------------------------------------

def test_standalone_mode_has_no_sso_and_no_restrictions(api, tables, admin_token):
    assert api.get("/api/auth/sso/config").json() == {"mode": "standalone"}
    assert api.post("/api/auth/sso", json={"token": "x"}).status_code == 404
    assert master_link.feature_allowed("face_training") and master_link.max_cameras() is None
    me = api.get("/api/auth/me", headers=bearer(admin_token)).json()
    assert me["license"] == {"mode": "standalone"} and me["sso"] is False
    assert api.get("/api/visitor-records/settings", headers=bearer(admin_token)).status_code == 200


# --- sign-in --------------------------------------------------------------------------

def test_valid_launch_token_opens_an_smc_session(api, lenient, portal):
    r = sign_in(api, launch_token(portal))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sso"] is True and body["license"]["active"] is True
    h = bearer(body["token"])
    me = api.get("/api/auth/me", headers=h).json()
    assert me["sso"] is True and me["username"] == "smc_client"
    assert set(me["license"]["features"]) == set(ALL_SMC_FEATURES)
    # the existing SMC pages work with this session
    assert api.get("/api/cameras", headers=h).status_code == 200
    assert lenient.get("/api/dashboard/summary", headers=h).status_code not in (401, 403)
    assert api.get("/api/visitor-records/settings", headers=h).status_code == 200


def test_config_tells_the_frontend_where_to_sign_in(api, portal):
    assert api.get("/api/auth/sso/config").json() == {"mode": "master", "login_url": config.MASTER_PORTAL_LOGIN_URL}


@pytest.mark.parametrize("bad", [
    {"exp": int(time.time()) - 120, "iat": int(time.time()) - 180},   # expired
    {"aud": "deco_vision"},                                              # for another application
    {"iss": "someone-else"},                                             # not the Master Portal
    {"sub": "lic-welmont"},                                              # another client's license
    {"exp": int(time.time()) + 3600},                                    # far too long-lived
])
def test_invalid_launch_tokens_are_refused(api, portal, bad):
    r = sign_in(api, launch_token(portal, **bad))
    assert r.status_code == 401
    assert "token" not in r.json()


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


def test_suspension_ends_open_sessions_and_reactivation_allows_sign_in(api, portal):
    token = sign_in(api, launch_token(portal)).json()["token"]
    assert api.get("/api/cameras", headers=bearer(token)).status_code == 200
    portal.ent["status"] = "suspended"
    master_link.refresh()
    assert api.get("/api/cameras", headers=bearer(token)).status_code == 401
    portal.ent["status"] = "active"
    master_link.refresh()
    assert api.get("/api/cameras", headers=bearer(token)).status_code == 401   # that session is gone
    assert sign_in(api, launch_token(portal)).status_code == 200                 # a new sign-in works


def test_logout_ends_the_sso_session(api, portal):
    token = sign_in(api, launch_token(portal)).json()["token"]
    assert api.post("/api/auth/logout", headers=bearer(token)).status_code == 200
    assert api.get("/api/auth/me", headers=bearer(token)).status_code == 401


def test_session_for_another_license_is_dropped(api, portal, monkeypatch):
    token = sign_in(api, launch_token(portal)).json()["token"]
    monkeypatch.setattr(config, "MASTER_LICENSE_ID", "lic-other")
    assert api.get("/api/cameras", headers=bearer(token)).status_code == 401


def test_local_admin_login_still_works_in_master_mode(api, portal, admin_token):
    me = api.get("/api/auth/me", headers=bearer(admin_token)).json()
    assert me["sso"] is False and me["license"]["mode"] == "master"


# --- portal outages -------------------------------------------------------------------

def test_portal_outage_keeps_the_last_license_within_the_grace_period(api, portal, monkeypatch):
    portal.down = True
    assert master_link.refresh() is None
    assert master_link.license_problem() is None and master_link.feature_allowed("attendance")
    # once the last good answer is older than the grace period, everything locks
    monkeypatch.setattr(master_link, "_fetched_at", time.time() - (config.ENTITLEMENT_GRACE_HOURS * 3600 + 5))
    assert master_link.license_problem() is not None
    assert not master_link.feature_allowed("attendance")
    assert master_link.max_cameras() == 0


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
    "intrusion_detection": "/api/zones/restricted",
}


@pytest.mark.parametrize("feature", list(FEATURE_ROUTES))
def test_turning_a_feature_off_blocks_its_api_and_on_restores_it(api, lenient, portal, feature):
    token = sign_in(api, launch_token(portal)).json()["token"]
    h = bearer(token)
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


def test_feature_block_applies_to_the_local_admin_too(api, portal, admin_token):
    portal.ent["features"] = ["attendance"]
    master_link.refresh()
    assert api.get("/api/visitor-records/settings", headers=bearer(admin_token)).status_code == 403


def test_camera_still_is_not_blocked_with_desk_analytics(api, lenient, portal):
    """Zone editors everywhere use /cameras/{id}/frame; only desk routes need workforce analytics."""
    token = sign_in(api, launch_token(portal)).json()["token"]
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
    h = bearer(sign_in(api, launch_token(portal)).json()["token"])
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


def test_sso_session_row_is_bound_to_the_license(api, portal):
    token = sign_in(api, launch_token(portal)).json()["token"]
    s = auth.get_session(token)
    assert s["sso_license_id"] == LICENSE_ID and s["admin_user_id"] is None
