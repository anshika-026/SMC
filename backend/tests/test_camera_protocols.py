"""RTSP + RTMP camera sources: validation, storage, secrecy, the address each
camera's reader opens, FFmpeg input options, status, clean-up, and the path from
decoded frames into the existing person-detection / face-recognition interface.

No real camera or RTMP server is used: readers run against scripted captures.
What these tests cannot prove (a real RTMP server, real codecs, real latency) is
listed in the report."""

import multiprocessing
import os
import sqlite3
import time

import pytest

from app import camera_db, camera_stream, config, rtsp_reader, stream_source

from tests.conftest import bearer

RTMP = "rtmp://media.example.net:1935/live/SECRET-stream-key-123"


# --- helpers -----------------------------------------------------------------------

def _create(api, token, **extra):
    body = {"name": "Cam", "site": "Noida Site", **extra}
    return api.post("/api/cameras", json=body, headers=bearer(token))


def _rtsp_body(**extra):
    return {"host": "10.0.0.2", "port": 554, "user": "admin", "password": "pw", "stream_path": "/ch1", **extra}


# --- 1. existing RTSP cameras keep working -------------------------------------------

def test_a_camera_sent_without_a_protocol_is_rtsp_and_builds_the_same_url_as_before(api, admin_token):
    r = _create(api, admin_token, **_rtsp_body())
    assert r.status_code == 200, r.text
    cam = r.json()
    assert cam["protocol"] == "rtsp" and cam["host"] == "10.0.0.2" and cam["source_url_set"] is False
    conn = camera_db.get_camera_connection(cam["id"])
    assert camera_stream.build_source_url(conn) == "rtsp://admin:pw@10.0.0.2:554/ch1"
    assert camera_stream.build_rtsp_url(conn) == "rtsp://admin:pw@10.0.0.2:554/ch1"


def test_a_database_from_before_the_protocol_columns_migrates_without_touching_cameras(tmp_path, monkeypatch):
    db_file = tmp_path / "old.db"
    with sqlite3.connect(db_file) as c:
        c.execute("""CREATE TABLE cameras (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, site TEXT NOT NULL DEFAULT '',
            cam_code TEXT DEFAULT '', purpose TEXT DEFAULT 'GENERAL', host TEXT DEFAULT '', port INTEGER DEFAULT 554,
            user TEXT DEFAULT '', password TEXT DEFAULT '', stream_path TEXT DEFAULT '', vendor TEXT DEFAULT '',
            status TEXT DEFAULT 'inactive', live_feed_enabled INTEGER DEFAULT 1, attendance_tracking INTEGER DEFAULT 1)""")
        c.execute("INSERT INTO cameras (name, site, host, port, user, password, stream_path, status) "
                  "VALUES ('Old', 'S', '192.0.2.7', 8554, 'u', 'p', '/video', 'active')")
    monkeypatch.setattr(camera_db, "DB_PATH", db_file)
    camera_db.init_db()
    camera_db.init_db()                                   # idempotent
    cam = camera_db.get_camera(1)
    assert cam["protocol"] == "rtsp" and cam["host"] == "192.0.2.7" and cam["port"] == 8554 and cam["live"] is True
    assert camera_stream.build_source_url(camera_db.get_camera_connection(1)) == "rtsp://u:p@192.0.2.7:8554/video"


def test_editing_an_rtsp_camera_without_touching_the_source_keeps_its_login(api, admin_token):
    cam = _create(api, admin_token, **_rtsp_body()).json()
    r = api.put(f"/api/cameras/{cam['id']}", json={"name": "Renamed"}, headers=bearer(admin_token))
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    assert camera_stream.build_source_url(camera_db.get_camera_connection(cam["id"])) == "rtsp://admin:pw@10.0.0.2:554/ch1"


# --- 2/3. RTMP validation and storage; unsupported protocols rejected ----------------------

@pytest.mark.parametrize("url,problem", [
    ("", "Enter the RTMP link"),
    ("http://host/live/key", "must start with rtmp://"),
    ("rtsp://host/live/key", "must start with rtmp://"),
    ("rtmp:///live/key", "no server address"),
    ("rtmp://host:99999/live/key", "valid address"),
    ("rtmp://host", "application name"),
    ("rtmp://host/", "application name"),
    ("rtmp://host/live/KEYMARK with space", "not allowed"),
    ("rtmp://host/live/KEYMARK\nrtmp://other/x", "not allowed"),
    ("rtmp://host/live/KEYMARK" + "k" * 3000, "too long"),
])
def test_bad_rtmp_links_are_rejected_with_a_message_that_does_not_echo_the_link(api, admin_token, url, problem):
    r = _create(api, admin_token, protocol="rtmp", source_url=url)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert problem in detail
    assert "KEYMARK" not in detail                              # the message never repeats what was typed


def test_an_unsupported_protocol_is_rejected(api, admin_token):
    for bad in ("hls", "http", "RTSPS", "ftp", ""):
        r = _create(api, admin_token, protocol=bad, **_rtsp_body())
        assert r.status_code == 422, (bad, r.text)
    assert camera_db.list_cameras() == []
    with pytest.raises(stream_source.SourceError):
        stream_source.normalize_protocol("hls")
    assert stream_source.normalize_protocol(None) == "rtsp" and stream_source.normalize_protocol(" RTMP ") == "rtmp"


def test_an_rtmp_camera_is_stored_with_its_link_and_the_link_never_leaves_the_api(api, admin_token):
    r = _create(api, admin_token, protocol="rtmp", source_url=RTMP, password="ignored", user="ignored")
    assert r.status_code == 200, r.text
    cam = r.json()
    assert cam["protocol"] == "rtmp" and cam["host"] == "media.example.net" and cam["port"] == 1935
    assert cam["source_url_set"] is True and cam["live"] is True and cam["status"] == "active"
    assert cam["source_display"] == "rtmp://media.example.net:1935/live/***"
    everything = api.get("/api/cameras", headers=bearer(admin_token)).text + r.text
    assert "SECRET-stream-key-123" not in everything and "source_url\"" not in everything.replace("source_url_set", "")
    stored = camera_db.get_camera_connection(cam["id"])
    assert stored["source_url"] == RTMP and stored["password"] == "" and stored["user"] == ""
    assert camera_stream.build_source_url(stored) == RTMP


def test_an_rtmp_camera_needs_its_link_and_survives_a_restart_of_the_database_layer(api, admin_token):
    assert _create(api, admin_token, protocol="rtmp").status_code == 422
    cam = _create(api, admin_token, protocol="rtmp", source_url=RTMP).json()
    camera_db.init_db()                                    # what a backend restart does
    again = camera_db.get_camera(cam["id"])
    assert again["protocol"] == "rtmp" and again["source_url_set"] and again["live"]
    assert camera_stream.build_source_url(camera_db.get_camera_connection(cam["id"])) == RTMP


def test_editing_an_rtmp_camera_keeps_the_link_unless_a_new_one_is_sent(api, admin_token, monkeypatch):
    restarts = []
    monkeypatch.setattr(camera_stream, "get_stream", lambda cid: type("S", (), {"restart": lambda self: restarts.append(cid)})())
    cam = _create(api, admin_token, protocol="rtmp", source_url=RTMP).json()
    h = bearer(admin_token)
    # a blank link means "unchanged" (the form never shows the stored one)
    r = api.put(f"/api/cameras/{cam['id']}", json={"protocol": "rtmp", "source_url": "", "name": "N"}, headers=h)
    assert r.status_code == 200
    assert camera_db.get_camera_connection(cam["id"])["source_url"] == RTMP
    # a new link replaces it, validates, re-derives the host, and reconnects the camera
    new = "rtmp://other.example.org/app/NEWKEY"
    r = api.put(f"/api/cameras/{cam['id']}", json={"protocol": "rtmp", "source_url": new}, headers=h)
    assert r.status_code == 200 and r.json()["host"] == "other.example.org"
    assert camera_db.get_camera_connection(cam["id"])["source_url"] == new
    assert restarts == [cam["id"]]
    assert api.put(f"/api/cameras/{cam['id']}", json={"protocol": "rtmp", "source_url": "nonsense"}, headers=h).status_code == 422
    assert camera_db.get_camera_connection(cam["id"])["source_url"] == new
    # RTMP -> RTSP: the stored RTMP link is dropped
    r = api.put(f"/api/cameras/{cam['id']}", json={"protocol": "rtsp", **_rtsp_body()}, headers=h)
    assert r.status_code == 200 and r.json()["protocol"] == "rtsp"
    assert camera_db.get_camera_connection(cam["id"])["source_url"] == ""


def test_the_camera_list_never_carries_credentials_for_either_protocol(api, admin_token):
    _create(api, admin_token, **_rtsp_body())
    _create(api, admin_token, protocol="rtmp", source_url=RTMP)
    for cam in api.get("/api/cameras", headers=bearer(admin_token)).json():
        assert "password" not in cam and "source_url" not in cam


def test_camera_management_still_requires_an_admin(api):
    assert api.post("/api/cameras", json={"name": "x", "site": "y", "protocol": "rtmp", "source_url": RTMP}).status_code == 401


def test_the_redacted_form_hides_passwords_and_stream_keys():
    assert stream_source.redact_source("rtsp://admin:pw@10.0.0.2:554/ch1") == "rtsp://admin:***@10.0.0.2:554/ch1"
    assert stream_source.redact_source("rtmp://u:pw@h:1935/live/KEY?token=T") == "rtmp://u:***@h:1935/live/***"
    assert stream_source.redact_source("rtmp://h/live") == "rtmp://h/live"
    from app import logging_setup

    line = logging_setup.redact(f"failed to open {RTMP} and rtsp://a:b@h/x")
    assert "SECRET-stream-key-123" not in line and ":b@" not in line


# --- 4. FFmpeg input options --------------------------------------------------------------

def test_ffmpeg_gets_rtmp_live_options_only_for_rtmp_and_they_do_not_leak(monkeypatch):
    import cv2

    seen = []

    class Cap:
        pass

    def fake_capture(url, backend, params):
        seen.append((url, backend, tuple(params), os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")))
        return Cap()

    monkeypatch.setattr(cv2, "VideoCapture", fake_capture)
    monkeypatch.delenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", raising=False)
    monkeypatch.delenv("RTSP_DECODE_THREADS", raising=False)
    rtsp_reader.open_capture(RTMP, 20.0, 10.0)
    rtsp_reader.open_capture("rtsp://u:p@h/x", 20.0, 10.0)
    (u1, b1, p1, o1), (u2, b2, p2, o2) = seen
    assert u1 == RTMP and b1 == cv2.CAP_FFMPEG and o1 == "rtmp_live;live"
    assert u2 == "rtsp://u:p@h/x" and o2 is None                     # RTSP behaviour unchanged
    assert p1 == (cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 20000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 10000, cv2.CAP_PROP_N_THREADS, 2)
    assert p2[-2:] == (cv2.CAP_PROP_N_THREADS, 2)                     # decoder threads bounded for every camera
    assert "OPENCV_FFMPEG_CAPTURE_OPTIONS" not in os.environ          # restored after the open
    monkeypatch.setenv("OPENCV_FFMPEG_CAPTURE_OPTIONS", "keep;me")
    rtsp_reader.open_capture("rtmps://h/app/k", 5.0, 5.0)
    assert os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] == "keep;me"


def test_test_stream_checks_the_link_before_connecting_and_never_echoes_it(api, admin_token, monkeypatch):
    opened = []
    monkeypatch.setattr(rtsp_reader, "open_capture", lambda *a: opened.append(a) or type("C", (), {
        "isOpened": lambda s: False, "release": lambda s: None})())
    h = bearer(admin_token)
    assert api.post("/api/cameras/test-stream", json={"url": "http://x/y"}, headers=h).status_code == 422
    assert api.post("/api/cameras/test-stream", json={"url": "rtmp://host"}, headers=h).status_code == 422
    assert opened == []                                                # invalid links are never opened
    r = api.post("/api/cameras/test-stream", json={"url": RTMP}, headers=h)
    assert r.status_code == 400 and "SECRET-stream-key-123" not in r.text and "rtmp://" not in r.text
    assert opened and opened[0][0] == RTMP
    old = api.post("/api/cameras/test-stream", json={"rtsp_url": "rtsp://u:p@h/x"}, headers=h)    # original field still works
    assert old.status_code == 400 and opened[-1][0] == "rtsp://u:p@h/x"


# --- 5-7, 11. handlers, status, clean-up, concurrency ----------------------------------------

@pytest.fixture
def streams(monkeypatch, tmp_path, isolated_db):
    """Three cameras (RTMP 911/912, RTSP 913) whose readers are scripted children."""
    cams = {
        911: {"id": 911, "protocol": "rtmp", "source_url": "rtmp://media.example.net/live/KEY911", "host": "media.example.net",
              "port": 1935, "user": "", "password": "", "stream_path": "", "attendance_tracking": 0, "live_feed_enabled": 1},
        912: {"id": 912, "protocol": "rtmp", "source_url": "rtmp://media.example.net/live/KEY912", "host": "media.example.net",
              "port": 1935, "user": "", "password": "", "stream_path": "", "attendance_tracking": 0, "live_feed_enabled": 1},
        913: {"id": 913, "protocol": "rtsp", "source_url": "", "host": "10.9.9.9", "port": 554, "user": "u", "password": "p",
              "stream_path": "/x", "attendance_tracking": 0, "live_feed_enabled": 1},
    }
    monkeypatch.setattr(config, "RTSP_CONNECT_TIMEOUT", 30.0)       # the scripted reader never reports a frame
    monkeypatch.setattr(config, "CAMERA_FRAME_TIMEOUT", 60.0)
    monkeypatch.setattr(camera_db, "get_camera_connection", lambda cid: cams.get(cid))
    monkeypatch.setattr(camera_db, "is_streamable", lambda c: True)
    monkeypatch.setenv("FAKE_READER_URL_DIR", str(tmp_path))
    yield camera_stream
    camera_stream.stop_all(timeout=8)
    for cid in cams:
        camera_stream._streams.pop(cid, None)


def _readers():
    return [p for p in multiprocessing.active_children() if p.name.startswith("rtsp-reader-") and p.is_alive()]


def _wait(cond, timeout=20):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class Sink:
    def put_nowait(self, item):
        pass


def test_each_camera_gets_one_reader_with_the_right_source_and_nothing_is_duplicated(streams, monkeypatch, tmp_path):
    from tests import fake_readers

    monkeypatch.setattr(rtsp_reader, "run", fake_readers.record_url_then_hang)
    subs = [Sink() for _ in range(3)]
    for cid, sink in zip((911, 912, 913), subs):
        streams.get_stream(cid).subscribe(sink, is_collector=True, wants_frames=False)
    assert _wait(lambda: len(list(tmp_path.glob("*.url"))) == 3), list(tmp_path.glob("*.url"))
    got = sorted(p.read_text() for p in tmp_path.glob("*.url"))
    assert got == sorted(["rtmp://media.example.net/live/KEY911", "rtmp://media.example.net/live/KEY912", "rtsp://u:p@10.9.9.9:554/x"])
    assert streams.get_stream(911) is streams.get_stream(911) and len(streams._streams) >= 3
    # a second viewer on a camera that is already running must not start a second reader
    streams.get_stream(911).subscribe(Sink(), wants_frames=False)
    time.sleep(1.0)
    assert len(_readers()) == 3 and sorted(p.name for p in _readers()) == ["rtsp-reader-911", "rtsp-reader-912", "rtsp-reader-913"]


def test_removing_a_camera_and_shutting_down_leave_no_reader_processes(streams, monkeypatch):
    from tests import fake_readers

    monkeypatch.setattr(rtsp_reader, "run", fake_readers.hang)
    for cid in (911, 912, 913):
        streams.get_stream(cid).subscribe(Sink(), is_collector=True, wants_frames=False)
    assert _wait(lambda: len(_readers()) == 3)
    streams.remove_stream(911)
    assert 911 not in streams._streams
    assert _wait(lambda: sorted(p.name for p in _readers()) == ["rtsp-reader-912", "rtsp-reader-913"], 15), [p.name for p in _readers()]
    streams.stop_all(timeout=10)
    assert _wait(lambda: not _readers(), 15), [p.name for p in _readers()]


def test_deleting_a_camera_through_the_api_stops_its_stream(api, admin_token, monkeypatch):
    cam = _create(api, admin_token, protocol="rtmp", source_url=RTMP).json()
    removed = []
    monkeypatch.setattr(camera_stream, "remove_stream", lambda cid, *a, **k: removed.append(cid))
    assert api.delete(f"/api/cameras/{cam['id']}", headers=bearer(admin_token)).status_code == 200
    assert removed == [cam["id"]] and camera_db.get_camera(cam["id"]) is None


def test_status_reports_connecting_live_reconnecting_and_failed_without_the_address(streams):
    s = camera_stream.CameraStream(914)
    secret_reason = "failed to open stream (check address, credentials, network)"
    s._handle_reader_message(("status", "connecting", {"attempt": 1}))
    assert s.state == camera_stream.S_CONNECTING
    s._handle_reader_message(("status", "connected", {}))
    assert s.state == camera_stream.S_RUNNING and s.connect_failures == 0
    s._handle_reader_message(("status", "reconnecting", {"attempt": 2, "delay": 1.0, "reason": secret_reason}))
    assert s.state == camera_stream.S_RECONNECTING and s.connect_failures == 2 and s.state_detail == secret_reason
    s._handle_reader_message(("status", "reconnecting", {"attempt": config.RTSP_MAX_RETRIES, "reason": secret_reason}))
    assert s.state == camera_stream.S_FAILED
    s._handle_reader_message(("status", "connected", {}))
    assert s.state == camera_stream.S_RUNNING                              # recovers
    assert "rtmp" not in repr(s.status()).lower() and "key" not in repr(s.status()).lower().replace("check", "")


# --- 9/10. decoded frames reach the existing AI interface -------------------------------------------

def test_decoded_rtmp_frames_reach_the_existing_face_pipeline_and_person_detection(streams, monkeypatch, tmp_path):
    """RTMP camera -> real rtsp_reader (scripted capture) -> shared memory ->
    CameraStream loop -> the SAME hooks an RTSP camera uses: the face pipeline's
    feed_frame (person overlay, face detection/recognition, tracking) and the
    shared person detector (footfall / staff count)."""
    from tests import fake_readers

    from app import analytics_settings, face_pipeline, person_detection

    monkeypatch.setattr(rtsp_reader, "run", fake_readers.run_real_reader_on_fake_source)
    fed, detected = [], []

    class RecordingPipeline:
        process_ms = None

        def feed_frame(self, frame, has_viewer=True):
            fed.append((frame.shape, int(frame.mean()), has_viewer))

    monkeypatch.setattr(camera_stream, "get_pipeline", lambda cid: RecordingPipeline())
    monkeypatch.setattr(person_detection.service, "feed", lambda cid, frame: detected.append((cid, frame.shape)))
    monkeypatch.setattr(analytics_settings, "enabled", lambda name: True)
    streams_cams = camera_db.get_camera_connection
    cam = dict(streams_cams(911), attendance_tracking=1)                  # analytics on for this camera
    monkeypatch.setattr(camera_db, "get_camera_connection", lambda cid: cam if cid == 911 else streams_cams(cid))
    monkeypatch.setattr(config, "RTSP_CONNECT_TIMEOUT", 20.0)

    s = streams.get_stream(911)
    s.subscribe(Sink(), wants_frames=True)
    assert _wait(lambda: fed and detected, 30), (fed, detected, s.status())
    assert fed[0][0] == (24, 32, 3) and fed[0][1] == 7                       # the RTMP-decoded pixels, unchanged
    assert detected[0] == (911, (24, 32, 3))
    assert s.state == camera_stream.S_RUNNING and s.face_health.snapshot() is not None
    assert face_pipeline.CameraFacePipeline.feed_frame                      # still the existing entry point


def test_analytics_off_stays_off_for_an_rtmp_camera(streams, monkeypatch):
    """attendance_tracking = 0 means live view only for RTSP; RTMP follows the same rule."""
    from tests import fake_readers

    from app import analytics_settings

    monkeypatch.setattr(rtsp_reader, "run", fake_readers.run_real_reader_on_fake_source)
    fed = []
    monkeypatch.setattr(camera_stream, "get_pipeline", lambda cid: type("P", (), {"process_ms": None, "feed_frame": lambda s, f, has_viewer=True: fed.append(1)})())
    monkeypatch.setattr(analytics_settings, "enabled", lambda name: True)
    s = streams.get_stream(912)                                          # attendance_tracking = 0
    s.subscribe(Sink(), wants_frames=True)
    assert _wait(lambda: s.state == camera_stream.S_RUNNING, 30)
    time.sleep(1.0)
    assert fed == []
