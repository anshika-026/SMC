"""Footfall evidence: one ~10 s clip per counted person per day (instead of a
still), and the registered employee's name when face recognition confirms
them ("Person" otherwise, never "Unknown"). Counting itself must not change.

Reuses the real FootfallService / reid_worker setup from test_footfall_gates
(fake detector and embedder; no camera or model). The face pipeline is faked
at face_pipeline.get_existing_pipeline.
"""

import time

import pytest

from app import employee_directory, face_pipeline, footfall, retention
from app.reid import reid_db
from tests.test_footfall_gates import GATE_1, GATE_3, FakeEmbedder, FakeTracker, svc, walk_past  # noqa: F401 (svc is a fixture)


class FakePipe:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def _identify_head(self, frame, head_box):
        self.calls.append(head_box)
        employee, confidence = self.answer
        return employee, confidence, "match" if employee else "no face"


@pytest.fixture
def pipe(monkeypatch):
    p = FakePipe((None, 0.0))
    monkeypatch.setattr(face_pipeline, "get_existing_pipeline", lambda cid: p)
    monkeypatch.setattr(employee_directory, "get_display", lambda eid: ({"EMP-1": "Asha Rao"}.get(eid), "#00f"))
    return p


def wait_identify(service):
    if service._identify_future is not None:
        service._identify_future.result(timeout=10)


def rows():
    with reid_db.get_connection() as conn:
        return conn.execute("SELECT person_id, camera_id, employee_id, clip_status FROM footfall_clips ORDER BY id").fetchall()


def test_one_clip_per_person_per_day_however_often_they_are_seen(svc, pipe):
    walk_past(svc, GATE_1, person=1, track_id=1)
    walk_past(svc, GATE_3, person=1, track_id=7)        # same person, another gate
    walk_past(svc, GATE_1, person=1, track_id=9)        # and back again
    assert svc.summary()["unique_today"] == 1           # counting unchanged
    assert len(svc.clip_requests) == 1 and len(rows()) == 1
    camera_id, t0, final, _done = svc.clip_requests[0]
    assert camera_id == GATE_1 and abs(t0 - time.time()) < 60 and final.suffix == ".mp4"


def test_two_people_two_clips(svc, pipe):
    walk_past(svc, GATE_1, person=1, track_id=1)
    walk_past(svc, GATE_1, person=2, track_id=2)
    assert svc.summary()["unique_today"] == 2
    assert len(svc.clip_requests) == 2


def test_recognised_employee_shows_their_registered_name(svc, pipe):
    pipe.answer = ("EMP-1", 0.96)
    walk_past(svc, GATE_1, person=1, track_id=1)
    wait_identify(svc)
    v = svc.summary()["visitors"]
    assert len(v) == 1 and v[0]["name"] == "Asha Rao" and v[0]["employee_id"] == "EMP-1"
    assert v[0]["clip_camera"] == "Gate 1" and v[0]["clip_at"] is not None
    x1, y1, x2, y2 = pipe.calls[0]                      # only the head part of the body box
    body = [20 + 150, 50, 20 + 150 + 110, 330]
    assert (x1, y1, x2) == (body[0], body[1], body[2]) and y2 < body[1] + 0.5 * (body[3] - body[1])


def test_unconfident_identity_is_person_never_a_guess(svc, pipe):
    pipe.answer = (None, 0.0)
    walk_past(svc, GATE_1, person=1, track_id=1)
    wait_identify(svc)
    v = svc.summary()["visitors"][0]
    assert v["name"] == "Person" and v["employee_id"] is None


def test_without_face_recognition_on_the_camera_it_is_person(svc, monkeypatch):
    monkeypatch.setattr(face_pipeline, "get_existing_pipeline", lambda cid: None)
    walk_past(svc, GATE_1, person=1, track_id=1)
    wait_identify(svc)
    assert svc.summary()["visitors"][0]["name"] == "Person"


def test_identification_is_rate_limited_and_stops_once_known(svc, pipe):
    pipe.answer = ("EMP-1", 0.96)
    walk_past(svc, GATE_1, person=1, track_id=1, frames=12)
    wait_identify(svc)
    assert len(pipe.calls) == 1                         # 12 looks, one identification
    walk_past(svc, GATE_1, person=1, track_id=1, frames=12)
    wait_identify(svc)
    assert len(pipe.calls) == 1                         # that track is named: no more work on it
    walk_past(svc, GATE_3, person=1, track_id=4, frames=12)
    wait_identify(svc)
    # A new track is tried once (Re-ID doesn't know yet who it is), never per frame.
    assert len(pipe.calls) == 2


def test_clip_ready_failed_and_served(svc, pipe):
    walk_past(svc, GATE_1, person=1, track_id=1)
    _cam, _t0, final, done = svc.clip_requests[0]
    clip_id = svc.summary()["visitors"][0]["clip_id"]
    assert svc.clip_file(clip_id) == (None, "pending")
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"\x00" * 100)
    done("ready", frames=60)
    path, reason = svc.clip_file(clip_id)
    assert reason == "ok" and path == final.resolve()
    assert svc.summary()["visitors"][0]["clip_status"] == "ready"

    walk_past(svc, GATE_1, person=2, track_id=2)
    done2 = svc.clip_requests[1][3]
    done2("failed", error="No video was available from this camera (it may have been offline).")
    other = [v for v in svc.summary()["visitors"] if v["clip_id"] != clip_id][0]
    assert other["clip_status"] == "failed" and "No video" in other["clip_error"]
    assert svc.clip_file(other["clip_id"]) == (None, "failed")


def test_clips_expire_with_the_pictures_but_the_person_stays(svc, pipe):
    pipe.answer = ("EMP-1", 0.96)
    walk_past(svc, GATE_1, person=1, track_id=1)
    wait_identify(svc)
    _cam, _t0, final, done = svc.clip_requests[0]
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"\x00" * 100)
    done("ready", frames=60)
    assert retention.purge_footfall_clips(time.time() + 1) == 1
    assert not final.exists()
    v = svc.summary()["visitors"][0]
    assert v["clip_status"] == "expired" and v["name"] == "Asha Rao"
    assert svc.summary()["unique_today"] == 1


def test_reset_removes_clips_with_the_identities(svc, pipe):
    walk_past(svc, GATE_1, person=1, track_id=1)
    assert len(rows()) == 1
    svc.reset()
    assert rows() == []
    for cid, scene in svc.scenes.items():               # runners were dropped by reset; rebuild like feed() would
        runner = footfall._GateRunner(cid, svc._gallery, svc._generation)
        runner.state.tracker, runner.state.embedder = FakeTracker(scene), FakeEmbedder(scene)
        svc._runners[cid] = runner
    walk_past(svc, GATE_1, person=1, track_id=11)       # counted again after a reset: a new clip
    assert len(rows()) == 1 and len(svc.clip_requests) == 2
