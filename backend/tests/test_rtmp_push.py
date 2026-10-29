"""RTMP push: generated stream keys, setup details, the ingest auth hook,
camera/customer mapping, status, key rotation, and the reader/AI path of a
pushed stream. The ingest server itself is not started here (see the report
for the end-to-end run against a real MediaMTX); its API calls are stubbed."""

import time

import pytest

from app import camera_db, camera_stream, config, ingest, license_db, rtsp_reader, stream_source
from tests.conftest import bearer

HOOK = "test-hook-secret-0123456789"
READER_PW = "reader-pw-0123456789"


@pytest.fixture
def push_env(monkeypatch):
    monkeypatch.setattr(config, "RTMP_PUBLIC_HOST", "ingest.example.net")
    monkeypatch.setattr(config, "RTMP_PUBLIC_PORT", 1935)
    monkeypatch.setattr(config, "RTMP_PUBLIC_SCHEME", "rtmp")
    monkeypatch.setattr(config, "RTMP_APP", "live")
    monkeypatch.setattr(config, "RTMP_FULL_URL_FORMAT", "{server}/{key}")
    monkeypatch.setattr(config, "INGEST_HOOK_SECRET", HOOK)
    monkeypatch.setattr(config, "INGEST_READ_USER", "smc-reader")
    monkeypatch.setattr(config, "INGEST_READ_PASSWORD", READER_PW)
    monkeypatch.setattr(config, "INGEST_RTSP_BASE", "rtsp://mediamtx:8554")
    monkeypatch.setattr(ingest, "_failed_publish", ingest.SlidingWindowLimiter(ingest.FAILED_PUBLISH_LIMIT, 300))
    monkeypatch.setattr(ingest, "ingest_ready", lambda key: None)        # no ingest server in unit tests
    kicked = []
    monkeypatch.setattr(ingest, "kick_publishers", lambda key: kicked.append(key) or 1)
    restarted = []

    class _Stream:
        last_frame_at = None

        def restart(self):
            restarted.append(True)

        def stop(self):
            pass

        def resume(self):
            pass

        # ingest.ensure_reading subscribes a keep-alive reader on an allowed publish.
        def subscribe(self, *args, **kwargs):
            pass

        def unsubscribe(self, *args, **kwargs):
            pass

    monkeypatch.setattr(camera_stream, "get_stream", lambda cid: _Stream())
    return {"kicked": kicked, "restarted": restarted}


def _push(api, token, name="Gate"):
    r = api.post("/api/cameras", json={"name": name, "site": "Noida Site", "protocol": "rtmp_push"}, headers=bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


def _hook(api, body, secret=HOOK):
    return api.post(f"/api/ingest/auth?secret={secret}", json=body)


def _publish(key, ip="198.51.100.7", path=None):
    return {"user": "", "password": "", "ip": ip, "action": "publish", "path": path or f"live/{key}", "protocol": "rtmp", "id": "x", "query": ""}


# --- 1-3. creation, uniqueness, URLs ----------------------------------------------------

def test_creating_a_push_camera_generates_its_key_and_returns_real_setup_details(api, admin_token, push_env):
    cam = _push(api, admin_token)
    setup = cam["rtmp_setup"]
    key = setup["stream_key"]
    assert stream_source.is_stream_key(key)
    assert setup["server_url"] == "rtmp://ingest.example.net/live"
    assert setup["full_url"] == f"rtmp://ingest.example.net/live/{key}"
    assert cam["protocol"] == "rtmp_push" and cam["rtmp_key_set"] and cam["live"] and cam["is_configured"]
    stored = camera_db.get_camera_connection(cam["id"])
    assert stored["rtmp_stream_key"] == key and stored["source_url"] == "" and stored["password"] == ""
    # the key is never part of an ordinary camera listing
    listing = api.get("/api/cameras", headers=bearer(admin_token)).text
    assert key not in listing and "rtmp_stream_key" not in listing


def test_keys_are_unique_unguessable_and_the_database_refuses_a_duplicate(api, admin_token, push_env):
    keys = {_push(api, admin_token, f"C{i}")["rtmp_setup"]["stream_key"] for i in range(25)}
    assert len(keys) == 25 and all(stream_source.is_stream_key(k) for k in keys)
    assert len({stream_source.generate_stream_key() for _ in range(2000)}) == 2000
    import sqlite3

    a, b = (c["id"] for c in camera_db.list_cameras()[:2])
    with pytest.raises(sqlite3.IntegrityError):
        camera_db.set_stream_key(b, camera_db.get_camera_connection(a)["rtmp_stream_key"])


@pytest.mark.parametrize("scheme,port,fmt,server,full", [
    ("rtmp", 1935, "{server}/{key}", "rtmp://h.example/live", "rtmp://h.example/live/K"),
    ("rtmp", 1936, "{server}/{key}", "rtmp://h.example:1936/live", "rtmp://h.example:1936/live/K"),
    ("rtmps", 443, "{server}/{key}", "rtmps://h.example/live", "rtmps://h.example/live/K"),
    ("rtmp", 1935, "{server}?key={key}", "rtmp://h.example/live", "rtmp://h.example/live?key=K"),
])
def test_server_and_full_url_follow_the_configuration(scheme, port, fmt, server, full):
    out = stream_source.push_setup("K", "h.example", port, scheme, "live", fmt)
    assert out == {"server_url": server, "stream_key": "K", "full_url": full}


def test_no_public_host_means_no_made_up_address(api, admin_token, push_env, monkeypatch):
    monkeypatch.setattr(config, "RTMP_PUBLIC_HOST", "")
    cam = _push(api, admin_token)
    assert cam["rtmp_setup"] is None and "not configured" in cam["rtmp_setup_error"]
    r = api.get(f"/api/cameras/{cam['id']}/rtmp-setup", headers=bearer(admin_token))
    assert r.status_code == 503 and "RTMP_PUBLIC_HOST" in r.json()["detail"]


# --- 4. authorized retrieval, rotation, rate limits ------------------------------------------

def test_setup_details_are_admin_only_and_stable_across_requests(api, admin_token, push_env):
    cam = _push(api, admin_token)
    assert api.get(f"/api/cameras/{cam['id']}/rtmp-setup").status_code == 401
    r1 = api.get(f"/api/cameras/{cam['id']}/rtmp-setup", headers=bearer(admin_token)).json()
    r2 = api.get(f"/api/cameras/{cam['id']}/rtmp-setup", headers=bearer(admin_token)).json()
    assert r1["stream_key"] == r2["stream_key"] == cam["rtmp_setup"]["stream_key"]       # not regenerated
    assert r1["status"] in ("offline", "unknown")                                       # no fake "online"
    rtsp = api.post("/api/cameras", json={"name": "R", "site": "S", "host": "10.0.0.2"}, headers=bearer(admin_token)).json()
    assert api.get(f"/api/cameras/{rtsp['id']}/rtmp-setup", headers=bearer(admin_token)).status_code == 404


def test_a_client_login_cannot_see_or_rotate_keys(api, admin_token, push_env):
    cam = _push(api, admin_token)
    company = license_db.create_company("Acme")
    lic = license_db.create_license(company["id"], 2, "acme", username="client_acme", password="pass-acme-123456")
    license_db.assign_cameras(lic["id"], [cam["id"]])
    tok = api.post("/api/licenses/client-login", json={"username": "client_acme", "password": "pass-acme-123456"})
    assert tok.status_code == 200, tok.text
    h = bearer(tok.json()["session_token"])
    assert api.get(f"/api/cameras/{cam['id']}/rtmp-setup", headers=h).status_code in (401, 403)
    assert api.post(f"/api/cameras/{cam['id']}/rtmp-key/rotate", headers=h).status_code in (401, 403)


def test_rotating_a_key_invalidates_the_old_one_disconnects_it_and_is_rate_limited(api, admin_token, push_env):
    cam = _push(api, admin_token)
    old = cam["rtmp_setup"]["stream_key"]
    r = api.post(f"/api/cameras/{cam['id']}/rtmp-key/rotate", headers=bearer(admin_token))
    assert r.status_code == 200
    new = r.json()["stream_key"]
    assert new != old and stream_source.is_stream_key(new)
    assert push_env["kicked"] == [old] and push_env["restarted"]
    assert _hook(api, _publish(old)).status_code == 401
    assert _hook(api, _publish(new)).status_code == 200
    codes = [api.post(f"/api/cameras/{cam['id']}/rtmp-key/rotate", headers=bearer(admin_token)).status_code for _ in range(6)]
    assert codes.count(200) == 4 and codes[-1] == 429                               # 5 per 10 minutes


# --- 5-6. the auth hook: invalid keys, mapping, no cross-camera publishing ------------------

def test_hook_requires_its_shared_secret(api, admin_token, push_env):
    key = _push(api, admin_token)["rtmp_setup"]["stream_key"]
    assert _hook(api, _publish(key), secret="wrong").status_code == 401
    assert api.post("/api/ingest/auth", json=_publish(key)).status_code == 401
    assert _hook(api, _publish(key)).status_code == 200


def test_invalid_keys_and_arbitrary_stream_names_are_rejected(api, admin_token, push_env):
    key = _push(api, admin_token)["rtmp_setup"]["stream_key"]
    other = stream_source.generate_stream_key()
    for path in (f"live/{other}", "live/anything", "live/", f"other/{key}", f"live/{key}/x", key, f"live/{key}x"):
        assert _hook(api, _publish(key, path=path, ip="203.0.113.9")).status_code == 401, path
    # publishing the right key over anything but RTMP is refused
    assert _hook(api, {**_publish(key), "protocol": "rtsp"}).status_code == 401
    assert _hook(api, {**_publish(key), "protocol": "srt"}).status_code == 401


def test_a_key_maps_to_its_own_camera_and_customer_only(api, admin_token, push_env):
    a, b = _push(api, admin_token, "A"), _push(api, admin_token, "B")
    company = license_db.create_company("Acme")
    lic = license_db.create_license(company["id"], 2, "acme")
    license_db.assign_cameras(lic["id"], [a["id"]])
    ka, kb = a["rtmp_setup"]["stream_key"], b["rtmp_setup"]["stream_key"]
    da, db = ingest.decide(_publish(ka)), ingest.decide(_publish(kb))
    assert da.allowed and da.camera_id == a["id"] and db.allowed and db.camera_id == b["id"]
    # A's key cannot publish to B's stream: the stream name is the key itself
    assert not ingest.decide(_publish(ka, path=f"live/{kb}x")).allowed
    assert license_db.is_camera_assigned(lic["id"], a["id"]) and not license_db.is_camera_assigned(lic["id"], b["id"])


def test_reading_is_only_for_the_backend_and_only_known_streams(api, admin_token, push_env):
    key = _push(api, admin_token)["rtmp_setup"]["stream_key"]
    read = {"action": "read", "path": f"live/{key}", "protocol": "rtsp", "ip": "172.18.0.2"}
    assert _hook(api, {**read, "user": "smc-reader", "password": READER_PW}).status_code == 200
    assert _hook(api, {**read, "user": "smc-reader", "password": "nope"}).status_code == 401
    assert _hook(api, {**read, "user": "", "password": ""}).status_code == 401
    assert _hook(api, {**read, "path": "live/" + stream_source.generate_stream_key(), "user": "smc-reader", "password": READER_PW}).status_code == 401
    assert _hook(api, {"action": "playback", "path": f"live/{key}", "user": "smc-reader", "password": READER_PW}).status_code == 401


def test_switched_off_cameras_cannot_publish(api, admin_token, push_env):
    cam = _push(api, admin_token)
    api.put(f"/api/cameras/{cam['id']}", json={"live_feed_enabled": False}, headers=bearer(admin_token))
    assert _hook(api, _publish(cam["rtmp_setup"]["stream_key"])).status_code == 401


def test_guessing_keys_from_one_address_gets_that_address_blocked(api, admin_token, push_env):
    key = _push(api, admin_token)["rtmp_setup"]["stream_key"]
    for _ in range(ingest.FAILED_PUBLISH_LIMIT):
        _hook(api, _publish(stream_source.generate_stream_key(), ip="192.0.2.66"))
    assert _hook(api, _publish(key, ip="192.0.2.66")).status_code == 401           # blocked even with a valid key
    assert _hook(api, _publish(key, ip="192.0.2.67")).status_code == 200           # other addresses unaffected


def test_switching_a_camera_away_from_push_drops_its_key(api, admin_token, push_env):
    cam = _push(api, admin_token)
    key = cam["rtmp_setup"]["stream_key"]
    r = api.put(f"/api/cameras/{cam['id']}", json={"protocol": "rtsp", "host": "10.0.0.9", "port": 554, "stream_path": "/x"},
                headers=bearer(admin_token))
    assert r.status_code == 200 and r.json()["protocol"] == "rtsp"
    assert camera_db.get_camera_connection(cam["id"])["rtmp_stream_key"] is None
    assert key in push_env["kicked"] and _hook(api, _publish(key)).status_code == 401


# --- 7. status: online only on real frames -------------------------------------------------------

def test_status_is_online_only_when_frames_arrive(monkeypatch):
    class S:
        last_frame_at = None

    now = 1000.0
    monkeypatch.setattr(ingest, "ingest_ready", lambda key: False)
    assert ingest.stream_status(1, "k", S(), now)["status"] == "offline"
    monkeypatch.setattr(ingest, "ingest_ready", lambda key: True)
    assert ingest.stream_status(1, "k", S(), now)["status"] == "receiving"       # ingest has it, no frame yet
    s = S()
    s.last_frame_at = now - 3
    assert ingest.stream_status(1, "k", s, now)["status"] == "online"
    s.last_frame_at = now - 60                                                    # stream stopped
    monkeypatch.setattr(ingest, "ingest_ready", lambda key: False)
    assert ingest.stream_status(1, "k", s, now)["status"] == "offline"
    monkeypatch.setattr(ingest, "ingest_ready", lambda key: None)                 # ingest server unreachable
    assert ingest.stream_status(1, "k", S(), now)["status"] == "unknown"


# --- 8-9. the pushed stream goes through the existing reader / AI path ---------------------------

def test_a_push_camera_is_read_from_the_ingest_server_with_the_backend_account(push_env):
    key = stream_source.generate_stream_key()
    cam = {"protocol": "rtmp_push", "rtmp_stream_key": key, "host": stream_source.PUSH_HOST}
    url = camera_stream.build_source_url(cam)
    assert url == f"rtsp://smc-reader:{READER_PW}@mediamtx:8554/live/{key}"
    assert camera_stream.build_source_url({"protocol": "rtmp_push", "rtmp_stream_key": "short"}) is None
    from app import logging_setup

    assert key not in logging_setup.redact(f"open {url}") and READER_PW not in logging_setup.redact(f"open {url}")


def test_push_cameras_retry_quickly_while_the_camera_is_not_publishing_yet():
    assert camera_stream._reader_settings("rtmp_push")["reconnect_max_delay"] <= camera_stream.PUSH_RECONNECT_MAX_DELAY
    assert camera_stream._reader_settings("rtsp")["reconnect_max_delay"] == config.RTSP_RECONNECT_MAX_DELAY


def test_pushed_frames_reach_the_existing_face_pipeline_and_person_detection(monkeypatch, isolated_db, push_env):
    """Push camera -> (ingest server's RTSP copy, scripted here) -> the real reader ->
    the same CameraStream hooks RTSP cameras use."""
    import multiprocessing  # noqa: F401

    from tests import fake_readers

    from app import analytics_settings, person_detection

    monkeypatch.undo()                                   # use the real camera_stream.get_stream again
    monkeypatch.setattr(config, "INGEST_READ_PASSWORD", READER_PW)
    key = stream_source.generate_stream_key()
    cam = {"id": 931, "protocol": "rtmp_push", "rtmp_stream_key": key, "host": stream_source.PUSH_HOST, "port": 1935,
           "user": "", "password": "", "stream_path": "", "source_url": "", "attendance_tracking": 1, "live_feed_enabled": 1}
    monkeypatch.setattr(camera_db, "get_camera_connection", lambda cid: cam if cid == 931 else None)
    monkeypatch.setattr(camera_db, "is_streamable", lambda c: True)
    monkeypatch.setattr(rtsp_reader, "run", fake_readers.run_real_reader_on_fake_source)
    monkeypatch.setattr(config, "RTSP_CONNECT_TIMEOUT", 20.0)
    fed, detected = [], []
    monkeypatch.setattr(camera_stream, "get_pipeline", lambda cid: type("P", (), {
        "process_ms": None, "feed_frame": lambda self, f, has_viewer=True: fed.append(f.shape)})())
    monkeypatch.setattr(person_detection.service, "feed", lambda cid, frame: detected.append(cid))
    monkeypatch.setattr(analytics_settings, "enabled", lambda name: True)

    class Sink:
        def put_nowait(self, item):
            pass

    s = camera_stream.get_stream(931)
    try:
        s.subscribe(Sink(), wants_frames=True)
        end = time.monotonic() + 30
        while time.monotonic() < end and not (fed and detected):
            time.sleep(0.05)
        assert fed and fed[0] == (24, 32, 3) and detected and detected[0] == 931, s.status()
        assert s.state == camera_stream.S_RUNNING and s.last_frame_at
    finally:
        camera_stream.remove_stream(931)


def test_an_accepted_publish_starts_reading_the_camera_once(api, admin_token, push_env, monkeypatch):
    """Without this a pushed stream reaches the ingest server but never the backend."""
    started = []
    monkeypatch.setattr(ingest, "_reading", set())
    monkeypatch.setattr(ingest, "ensure_reading", lambda cid: started.append(cid) or True)
    cam = _push(api, admin_token)
    key = cam["rtmp_setup"]["stream_key"]
    assert _hook(api, _publish(key)).status_code == 200
    assert started == [cam["id"]]
    assert _hook(api, _publish(stream_source.generate_stream_key(), ip="203.0.113.50")).status_code == 401
    assert started == [cam["id"]]                                    # a refused publish starts nothing


def test_ensure_reading_subscribes_one_background_keepalive(monkeypatch):
    subs = []

    class S:
        def subscribe(self, sink, is_collector=False, wants_frames=True):
            subs.append((is_collector, wants_frames))

    monkeypatch.setattr(ingest, "_reading", set())
    monkeypatch.setattr(camera_stream, "get_stream", lambda cid: S())
    assert ingest.ensure_reading(5) is True and ingest.ensure_reading(5) is False
    assert subs == [(True, False)]                                   # one collector, no frames, no overlay cost
    ingest.forget(5)
    assert ingest.ensure_reading(5) is True
