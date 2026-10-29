"""Persistent person boxes: per-track expiry, smoothing/prediction, identity
hand-off to a replacement track, background recognition and rescue workers."""

import queue
import time
from concurrent.futures import Future

import numpy as np
import supervision as sv

from app import face_pipeline as fp
from app.face_pipeline import CameraFacePipeline, PersonTrackState

FRAME = np.zeros((1080, 1920, 3), dtype=np.uint8)
NO_FACES = np.empty((0, 5), dtype=np.float32)


def _pipeline(monkeypatch):
    """A pipeline with real tracking state but no models and no workers."""
    p = object.__new__(CameraFacePipeline)
    p.camera_id = 3
    p.frame_idx = 0
    p.person_tracks = {}
    p.person_tracker = sv.ByteTrack(track_activation_threshold=0.4, lost_track_buffer=fp.PERSON_TRACK_MAX_AGE,
                                    minimum_matching_threshold=fp.PERSON_TRACKER_MATCH_THRESHOLD)
    p._live_detections, p.last_overlay_at, p.frame_size = [], 0.0, (1920, 1080)
    p._last_rescue_frame = 0
    p._diag_last_log, p._diag_counts = {}, {}
    p._detections = np.empty((0, 5), dtype=np.float32)
    p._detect_persons = lambda frame: p._detections
    p._collect_rescue = lambda frame: np.empty((0, 5), dtype=np.float32)
    p._update_appearance_identities = lambda *a, **k: None
    p._update_person_identity = lambda *a, **k: None
    monkeypatch.setattr(fp.employee_directory, "get_display", lambda emp: (None, "#2563eb"))
    monkeypatch.setattr(fp.expression.service, "get", lambda cam, tid: (None, 0.0))
    return p


def _det(x1, y1, x2, y2, conf=0.8):
    return np.array([[x1, y1, x2, y2, conf]], dtype=np.float32)


def _cycle(p, dets):
    p.frame_idx += 2
    p._detections = dets
    p._update_person_overlay(FRAME, NO_FACES)


# ---- visibility ------------------------------------------------------------------

def test_a_slow_pipeline_does_not_blank_the_boxes_but_a_departed_person_expires(monkeypatch):
    p = _pipeline(monkeypatch)
    _cycle(p, _det(100, 100, 300, 600))
    assert len(p.get_live_detections()) == 1
    # the pipeline has not finished a cycle for 4 s: the person is still shown
    p.last_overlay_at -= 4.0
    p._live_detections[0]["_last_seen"] -= 4.0
    assert len(p.get_live_detections()) == 1
    # ...and drops off on its own once they have really been gone longer than the timeout
    p._live_detections[0]["_last_seen"] -= fp.PERSON_TRACK_MAX_SECONDS
    assert p.get_live_detections() == []


def test_a_weak_detection_in_the_middle_does_not_remove_the_person(monkeypatch):
    p = _pipeline(monkeypatch)
    for _ in range(3):
        _cycle(p, _det(100, 100, 300, 600, 0.8))
    _cycle(p, np.empty((0, 5), dtype=np.float32))        # detector misses this cycle
    shown = p.get_live_detections()
    assert len(shown) == 1 and shown[0]["employee_id"] is None
    _cycle(p, _det(104, 100, 304, 600, 0.35))            # low-confidence re-detection
    again = p.get_live_detections()
    assert len(again) == 1 and again[0]["track_id"] == shown[0]["track_id"]   # same track, no duplicate


def test_two_people_get_two_tracks_and_names_do_not_cross(monkeypatch):
    p = _pipeline(monkeypatch)
    both = np.array([[100, 100, 300, 600, 0.9], [1200, 100, 1400, 600, 0.9]], dtype=np.float32)
    _cycle(p, both)
    ids = sorted(p.person_tracks)
    assert len(ids) == 2
    left = min(p.person_tracks.values(), key=lambda s: s.bbox[0])
    left.current_identity, left.last_recognized_time = "7", time.time()
    for _ in range(3):
        _cycle(p, both)
    names = {tuple(d["bbox"][:1])[0] < 960: d["employee_id"] for d in p.get_live_detections()}
    assert names == {True: "7", False: None}


# ---- identity ---------------------------------------------------------------------

def test_a_name_survives_the_track_being_replaced_by_a_new_id_at_the_same_place(monkeypatch):
    p = _pipeline(monkeypatch)
    old = PersonTrackState(track_id=99, bbox=(100, 100, 300, 600), last_seen_time=time.time(),
                           current_identity="7", identity_confidence=0.93, last_recognized_time=time.time())
    old.disp_box = (100.0, 100.0, 300.0, 600.0)
    p.person_tracks[99] = old                               # coasting: not detected this cycle
    _cycle(p, _det(110, 105, 310, 605))                     # ByteTrack opens a new id for the same person
    assert 99 not in p.person_tracks and len(p.person_tracks) == 1
    shown = p.get_live_detections()
    assert len(shown) == 1 and shown[0]["employee_id"] == "7" and shown[0]["confidence"] == 0.93


def test_a_name_is_kept_while_the_face_is_not_visible(monkeypatch):
    p = _pipeline(monkeypatch)
    _cycle(p, _det(100, 100, 300, 600))
    st = next(iter(p.person_tracks.values()))
    st.current_identity, st.last_recognized_time = "7", time.time() - 120      # last face seen 2 minutes ago
    for _ in range(3):
        _cycle(p, _det(100, 100, 300, 600))
    assert p.get_live_detections()[0]["employee_id"] == "7"


def test_a_name_is_cleared_when_the_person_leaves(monkeypatch):
    p = _pipeline(monkeypatch)
    _cycle(p, _det(100, 100, 300, 600))
    next(iter(p.person_tracks.values())).current_identity = "7"
    st = next(iter(p.person_tracks.values()))
    st.last_recognized_time = time.time()
    st.last_seen_time -= fp.PERSON_TRACK_MAX_SECONDS + 1
    _cycle(p, np.empty((0, 5), dtype=np.float32))
    assert p.person_tracks == {} and p.get_live_detections() == []


# ---- smoothing / prediction --------------------------------------------------------------

def test_box_is_smoothed_and_a_jump_resets_instead_of_smearing():
    st = PersonTrackState(track_id=1, last_seen_time=time.time() - 0.5)
    CameraFacePipeline._smooth_box(st, (100, 100, 200, 300), time.time())
    CameraFacePipeline._smooth_box(st, (140, 100, 240, 300), time.time())
    assert 100 < st.disp_box[0] < 140                       # moved towards the new box, not onto it
    CameraFacePipeline._smooth_box(st, (1500, 700, 1600, 900), time.time())
    assert st.disp_box == (1500.0, 700.0, 1600.0, 900.0) and st.vel == (0.0, 0.0)


def test_prediction_is_short_damped_and_inside_the_frame(monkeypatch):
    p = _pipeline(monkeypatch)
    _cycle(p, _det(1700, 100, 1900, 600))
    d = p._live_detections[0]
    d["_vel"] = (400.0, 0.0)                                # heading out of the picture
    d["_last_seen"] = time.time() - 10.0                    # but expired -> hidden
    assert p.get_live_detections() == []
    d["_last_seen"] = time.time() - 0.9
    box = p.get_live_detections()[0]["bbox"]
    assert box[0] > 1700 and box[2] <= 1920                 # moved on a little, clamped to the frame


# ---- background workers --------------------------------------------------------------------

def test_recognition_work_is_queued_once_per_person_and_never_blocks(monkeypatch):
    p = _pipeline(monkeypatch)
    p._update_person_identity = lambda *a, **k: CameraFacePipeline._update_person_identity(p, *a, **k)
    p._refresh_gallery = lambda: None
    p._gallery_cache = [{"person_id": "7", "embedding": [0.0]}]
    p._get_classifier = lambda: None
    p._recog_queue = queue.Queue(maxsize=2)
    p.frame_idx = 10
    monkeypatch.setattr(fp.analytics_settings, "enabled", lambda name: False)
    row = np.array([100, 100, 160, 160, 0.9], dtype=np.float32)
    a, b, c = (PersonTrackState(track_id=i) for i in (1, 2, 3))
    for st in (a, b):
        p._update_person_identity(st, FRAME, row)
    p._update_person_identity(a, FRAME, row)                # same person again: skipped (in flight)
    p._update_person_identity(c, FRAME, row)                # queue full: skipped, not blocked
    assert p._recog_queue.qsize() == 2 and a.recog_inflight and b.recog_inflight and not c.recog_inflight


def test_rescue_sweep_never_blocks_and_late_or_overlapping_results_are_dropped(monkeypatch):
    p = _pipeline(monkeypatch)
    p._collect_rescue = lambda frame: CameraFacePipeline._collect_rescue(p, frame)

    class _Pool:
        def __init__(self):
            self.submitted = 0

        def submit(self, fn, frame):
            self.submitted += 1
            return fut

    fut = Future()
    p._rescue_pool, p._rescue_future = _Pool(), None
    p.frame_idx = fp.PERSON_RESCUE_INTERVAL_FRAMES + 1
    assert len(p._collect_rescue(FRAME)) == 0 and p._rescue_pool.submitted == 1      # started, did not wait
    assert len(p._collect_rescue(FRAME)) == 0 and p._rescue_pool.submitted == 1      # still running: no second sweep
    seated = np.array([[500, 200, 700, 800, 0.4]], dtype=np.float32)
    existing = PersonTrackState(track_id=5, bbox=(502, 202, 702, 802), last_seen_time=time.time())
    p.person_tracks[5] = existing
    fut.set_result((np.vstack([seated, [[1500, 200, 1700, 800, 0.4]]]).astype(np.float32), time.time()))
    got = p._collect_rescue(FRAME)
    assert len(got) == 1 and got[0][0] == 1500                                       # the already-shown person is not duplicated

    stale = Future()
    stale.set_result((seated, time.time() - fp.PERSON_RESCUE_RESULT_MAX_AGE - 1))
    p._rescue_future = stale
    p.frame_idx = 0
    assert len(p._collect_rescue(FRAME)) == 0                                        # too old to describe this scene


def test_looking_up_a_pipeline_never_waits_for_one_being_built(monkeypatch):
    """/ws/detections runs on the server's event loop: if its lookup waited for a
    pipeline whose models are loading, the whole API froze (seen live)."""
    import threading

    started, release = threading.Event(), threading.Event()

    class Slow:
        def __init__(self, camera_id):
            started.set()
            release.wait(10)                      # a model load that takes a while

    monkeypatch.setattr(fp, "CameraFacePipeline", Slow)
    monkeypatch.setattr(fp, "_pipelines", {})
    monkeypatch.setattr(fp, "_creating", {})
    t = threading.Thread(target=fp.get_pipeline, args=(77,), daemon=True)
    t.start()
    assert started.wait(5)
    t0 = time.monotonic()
    assert fp.get_existing_pipeline(77) is None and fp.get_existing_pipeline(78) is None
    other = threading.Thread(target=lambda: fp.discard_pipeline(78), daemon=True)
    other.start()
    other.join(2)
    assert time.monotonic() - t0 < 0.5 and not other.is_alive()   # nothing blocked behind the build
    release.set()
    t.join(5)
    assert isinstance(fp.get_existing_pipeline(77), Slow)
    assert fp.get_pipeline(77) is fp.get_existing_pipeline(77)      # built once, reused
