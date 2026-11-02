"""Evidence clips for intrusion / loitering alerts (evidence.py, ported from MAINDV and built on
SMC's shared clip recorder, clips.py), the clip endpoint, clip retention, and the Intrusion
page's alert history / analytics endpoints."""

import time

import cv2
import numpy as np
import pytest

from app import alerts, camera_db, clips, evidence, insights, intrusion, retention
from tests.conftest import bearer

SQUARE = [[0.1, 0.1], [0.6, 0.1], [0.6, 0.9], [0.1, 0.9]]


def frame(i):
    img = np.zeros((240, 320, 3), dtype=np.uint8)
    cv2.putText(img, str(i), (100, 140), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 3)
    return img


@pytest.fixture
def env(isolated_db, tmp_path, monkeypatch):
    alerts.init_db()
    intrusion.init_db()
    monkeypatch.setattr(alerts, "SNAPSHOT_DIR", tmp_path / "alert_snapshots")
    rec = clips.ClipRecorder()
    svc = evidence.ClipService(recorder=rec, clip_dir=tmp_path / "alert_clips")
    monkeypatch.setattr(evidence, "service", svc)
    cam = camera_db.add_camera("Lobby", "HQ")
    return rec, svc, cam, tmp_path


def new_alert(cam, event="Intrusion Detected", zone="Main Door", ts=None, zone_id=None):
    return alerts.raise_alert(event, "High", cam, message="1 person inside", now=ts or time.time(),
                              zone_id=zone_id, zone_name=zone, on_created=lambda aid: None)


def buffered(rec, cam, t0, n=30, spacing=0.3):
    """Frames from t0-4.5 s on, as the camera loop would have tapped them."""
    rec.want(evidence.OWNER, {cam})
    for i in range(n):
        rec._buffer_frame(cam, frame(i), t0 - 4.5 + i * spacing)


def wait_status(aid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = alerts.clip_info(aid)
        if info["status"] != "pending":
            return info
        time.sleep(0.2)
    return alerts.clip_info(aid)


def test_clip_is_about_ten_seconds_h264_playable_and_stored_safely(env):
    rec, svc, cam, tmp = env
    t0 = time.time() - 8                      # the post-event seconds are already past
    buffered(rec, cam, t0)
    aid = new_alert(cam, ts=t0)
    assert alerts.clip_info(aid)["status"] == "pending"
    svc.schedule(aid, cam, t0)
    info = wait_status(aid)
    assert info["status"] == "ready" and info["path"].parent == tmp / "alert_clips"
    cap = cv2.VideoCapture(str(info["path"]))
    assert cap.read()[0] and cap.get(cv2.CAP_PROP_FRAME_COUNT) >= 20
    assert int(cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little") in (b"avc1", b"h264", b"H264")
    cap.release()
    row = alerts.list_alerts("all")[0]
    assert row["has_clip"] and row["clip_status"] == "ready" and "clip_path" not in row


def test_camera_not_buffered_means_unavailable_at_once(env):
    rec, svc, cam, tmp = env
    aid = new_alert(cam)
    svc.schedule(aid, cam, time.time())
    assert alerts.clip_info(aid)["status"] == "unavailable"


def test_no_footage_around_the_event_means_unavailable_and_no_file(env):
    rec, svc, cam, tmp = env
    rec.want(evidence.OWNER, {cam})          # buffered, but the camera gave nothing
    t0 = time.time() - 8
    aid = new_alert(cam, ts=t0)
    svc.schedule(aid, cam, t0)
    assert wait_status(aid)["status"] == "unavailable"
    assert not list((tmp / "alert_clips").glob("*")) if (tmp / "alert_clips").exists() else True


def test_an_encoder_crash_marks_the_clip_failed_and_leaves_no_file(env, monkeypatch):
    rec, svc, cam, tmp = env
    monkeypatch.setattr(clips, "encode_mp4", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    t0 = time.time() - 8
    buffered(rec, cam, t0)
    aid = new_alert(cam, ts=t0)
    svc.schedule(aid, cam, t0)
    assert wait_status(aid)["status"] == "failed"
    assert not [p for p in (tmp / "alert_clips").glob("*") if p.suffix == ".mp4" and ".part" not in p.name]


def test_an_unplayable_file_is_never_reported_ready(env, monkeypatch):
    rec, svc, cam, tmp = env
    monkeypatch.setattr(evidence, "is_playable", lambda p: False)
    t0 = time.time() - 8
    buffered(rec, cam, t0)
    aid = new_alert(cam, ts=t0)
    svc.schedule(aid, cam, t0)
    assert wait_status(aid)["status"] == "failed"


def test_intrusion_alert_hands_its_clip_to_the_evidence_service(env, monkeypatch):
    """The real chain: a person's feet in a zone -> alert (pending clip) -> evidence.schedule."""
    rec, svc, cam, tmp = env
    asked = []
    monkeypatch.setattr(svc, "schedule", lambda aid, c, ts: asked.append((aid, c)))
    monkeypatch.setattr(intrusion.analytics_settings, "enabled", lambda f: True)
    z = intrusion.create_zone(cam, "Main Door", SQUARE, None, None, area="Reception", is_entry_gate=True)
    people = [{"bbox": [100, 50, 160, 200], "confidence": 0.9, "track_id": 1}]   # feet (130, 200) of 320x240
    intrusion.service._detect(cam, np.zeros((240, 320, 3), np.uint8), [intrusion.get_zone(z["id"])], time.time(),
                              [p["bbox"] for p in people], people)
    (row,) = alerts.list_alerts("all")
    assert row["event"] == "Intrusion Detected" and row["zone_name"] == "Main Door" and row["clip_status"] == "pending"
    assert asked == [(row["id"], cam)]


@pytest.fixture
def served(api, admin_token, env):
    rec, svc, cam, tmp = env
    t0 = time.time() - 8
    buffered(rec, cam, t0)
    aid = new_alert(cam, ts=t0)
    svc.schedule(aid, cam, t0)
    assert wait_status(aid)["status"] == "ready"
    return api, bearer(admin_token), aid, cam


def test_a_ready_clip_plays_and_downloads(served):
    api, h, aid, cam = served
    r = api.get(f"/api/alerts/{aid}/clip", headers=h)
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and len(r.content) > 1000
    d = api.get(f"/api/alerts/{aid}/clip?download=1", headers=h)
    assert "attachment" in d.headers["content-disposition"] and f"intrusion-detected-main-door-alert-{aid}.mp4" in d.headers["content-disposition"]


@pytest.mark.parametrize("status, code, words", [
    ("pending", 409, "still being prepared"),
    ("unavailable", 404, "No video was available"),
    ("failed", 404, "could not be saved"),
    ("expired", 404, "retention period"),
])
def test_clip_states_that_are_not_ready_say_so(api, admin_token, env, status, code, words):
    rec, svc, cam, tmp = env
    aid = new_alert(cam)
    alerts.set_clip(aid, None, status)
    r = api.get(f"/api/alerts/{aid}/clip", headers=bearer(admin_token))
    assert r.status_code == code and words in r.json()["detail"]


def test_alert_without_a_clip_or_unknown_alert_is_a_plain_404(api, admin_token, env):
    rec, svc, cam, tmp = env
    plain = alerts.raise_alert("Camera Offline", "Critical", cam, message="x")
    h = bearer(admin_token)
    assert api.get(f"/api/alerts/{plain}/clip", headers=h).status_code == 404
    assert api.get("/api/alerts/99999/clip", headers=h).status_code == 404


def test_clips_need_a_login(served):
    api, h, aid, cam = served
    assert api.get(f"/api/alerts/{aid}/clip").status_code == 401


def test_old_clips_are_removed_by_retention_but_the_alert_stays(served, monkeypatch):
    api, h, aid, cam = served
    path = alerts.clip_info(aid)["path"]
    monkeypatch.setattr(retention, "_inside_data_dir", lambda p: True)
    with alerts._conn() as conn:
        conn.execute("UPDATE alerts SET ts = ts - 30 * 86400 WHERE id = ?", (aid,))
    assert retention.purge_alert_clips(time.time()) == 1
    assert not path.exists() and alerts.clip_info(aid)["status"] == "expired"
    assert [a["id"] for a in alerts.list_alerts("all")] == [aid]


# ---- Intrusion page: alert history and analytics -------------------------------------------

def test_alert_history_filters_and_analytics_use_real_records(api, admin_token, env):
    rec, svc, cam, tmp = env
    other = camera_db.add_camera("Server room", "HQ")
    door = intrusion.create_zone(cam, "Reception Entry Gate", SQUARE, None, None, area="Reception", is_entry_gate=True)
    wait = intrusion.create_zone(other, "Waiting", SQUARE, None, None, zone_type="loitering", dwell_seconds=600)
    now = time.time()
    a1 = alerts.raise_alert("Intrusion Detected", "High", cam, "1 person inside Reception Entry Gate", dedupe_key="intrusion:1",
                            now=now - 3600, zone_id=door["id"], zone_name=door["name"])
    alerts.raise_alert("Intrusion Detected", "High", cam, "x", dedupe_key="intrusion:1", now=now - 3540)   # same open intrusion
    alerts.raise_alert("Loitering Detected", "Medium", other, "stayed", now=now - 60, zone_id=wait["id"], zone_name="Waiting")
    alerts.raise_alert("Camera Offline", "Critical", cam, "not an intrusion", now=now)
    h = bearer(admin_token)
    rows = api.get("/api/intrusion/alerts", headers=h).json()["alerts"]
    assert [r["event"] for r in rows] == ["Loitering Detected", "Intrusion Detected"]
    gate = rows[1]
    assert gate["id"] == a1 and gate["area"] == "Reception" and gate["is_entry_gate"] and gate["duration_seconds"] == pytest.approx(60)
    assert rows[0]["duration_seconds"] == 600                                    # loitering: at least the zone's limit
    assert [r["id"] for r in api.get(f"/api/intrusion/alerts?camera_id={other}", headers=h).json()["alerts"]] == [rows[0]["id"]]
    assert len(api.get("/api/intrusion/alerts?q=reception", headers=h).json()["alerts"]) == 1
    an = api.get("/api/intrusion/analytics", headers=h).json()
    assert (an["total"], an["intrusions"], an["loitering"]) == (2, 1, 1)
    assert {x["name"]: x["count"] for x in an["by_camera"]} == {"Lobby": 1, "Server room": 1}
    assert {x["name"] for x in an["by_area"]} == {"Reception", "No area"}
    assert sum(x["count"] for x in an["by_hour"]) == 2 and an["per_day"][-1]["count"] >= 1
    x = api.get("/api/intrusion/export", headers=h)
    assert x.status_code == 200 and x.content[:2] == b"PK"
    assert api.get("/api/intrusion/alerts?start=2026-11-05&end=2026-11-01", headers=h).status_code == 422


def test_dashboard_security_counts_intrusions(env, monkeypatch):
    rec, svc, cam, tmp = env
    monkeypatch.setattr(insights.analytics_settings, "available", lambda f: f in ("intrusion", "alerts"))   # only this section (and alerts themselves)
    alerts.raise_alert("Intrusion Detected", "High", cam, "x", now=time.time())
    alerts.raise_alert("Loitering Detected", "Medium", cam, "y", now=time.time())
    s = insights.business_overview()["security"]
    assert (s["intrusion"], s["loitering"], s["restricted_violations"], s["intrusion_open"]) == (2, 1, 1, 2)
