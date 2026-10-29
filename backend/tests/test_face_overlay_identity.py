"""Live-overlay identity: the closed-set classifier must not override a
confident gallery match to someone else, and failures are counted by reason."""

import numpy as np

from app import face_matching as fm
from app import face_pipeline
from app.face_pipeline import CameraFacePipeline

DIM = 512


def _unit(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).normal(size=DIM).astype(np.float32)
    return v / np.linalg.norm(v)


class _Clf:
    """Stands in for the trained sklearn classifier: always answers `cls` at `proba`."""

    def __init__(self, cls: str, proba: float, others=("1", "2")):
        self.classes_ = np.array([cls, *others])
        self._p = np.array([proba, *([(1 - proba) / len(others)] * len(others))])

    def predict_proba(self, x):
        return self._p.reshape(1, -1)


def _pipeline(gallery, clf=None):
    p = object.__new__(CameraFacePipeline)
    p.camera_id = 7
    p._gallery_cache = gallery
    p._gallery_loaded_at = 10**12            # never refreshed from the DB in tests
    p._gallery_index = fm.build_index(gallery)
    p._gallery_problems_logged = (0, 0, 0)
    p._diag_last_log, p._diag_counts, p._last_match = {}, {}, None
    p._get_classifier = lambda: clf
    return p


def _entry(pid, vec):
    return {"person_id": pid, "embedding": [float(x) for x in vec]}


def test_classifier_answer_for_a_different_enrolled_person_is_overridden_by_the_gallery():
    live = _unit(5)
    gallery = [_entry("13", live), _entry("1", _unit(1))]   # person 13 enrolled after training
    p = _pipeline(gallery, _Clf("1", 0.70))                 # classifier insists it is person 1
    who, score = p._identify_for_overlay(live)
    assert who == "13" and score > 0.9


def test_classifier_answer_stands_when_gallery_agrees_or_is_not_confident():
    live = _unit(5)
    agree = _pipeline([_entry("1", live), _entry("2", _unit(2))], _Clf("1", 0.80))
    assert agree._identify_for_overlay(live) == ("1", 0.80)
    unsure = _pipeline([_entry("2", _unit(2))], _Clf("1", 0.80))   # gallery has nobody close
    assert unsure._identify_for_overlay(live) == ("1", 0.80)


def test_low_confidence_classifier_falls_back_to_gallery_at_the_live_threshold():
    live = _unit(5)
    other = _unit(9)
    near = (0.5 * live + np.sqrt(1 - 0.25) * (other - np.dot(live, other) * live) /
            np.linalg.norm(other - np.dot(live, other) * live)).astype(np.float32)   # cosine ~0.5 to live
    p = _pipeline([_entry("13", near)], _Clf("1", 0.40))
    who, score = p._identify_for_overlay(live)
    assert who == "13" and 0.45 < score < 0.55


def test_nobody_close_enough_stays_unnamed_and_keeps_the_reason():
    p = _pipeline([_entry("13", _unit(1)), _entry("2", _unit(2))], None)
    assert p._identify_for_overlay(_unit(99)) == (None, 0.0)
    assert p._last_match.reason == fm.BELOW_MATCH_THRESHOLD


def test_diag_counts_every_failure_but_logs_only_when_enabled_and_rate_limited(monkeypatch, caplog):
    p = _pipeline([])
    monkeypatch.setattr(face_pipeline, "FACE_DIAG", False)
    p._diag("NO_EMBEDDING", face_w=20, face_h=30)
    assert p.diag_snapshot() == {"NO_EMBEDDING": 1} and not caplog.records

    monkeypatch.setattr(face_pipeline, "FACE_DIAG", True)
    monkeypatch.setattr(face_pipeline, "FACE_DIAG_INTERVAL_SECONDS", 3600.0)
    with caplog.at_level("INFO", logger="face_pipeline"):
        p._diag("NO_EMBEDDING", face_w=20, face_h=30, det_conf=0.81)
        p._diag("NO_EMBEDDING", face_w=21, face_h=30)           # rate-limited, still counted
        p._diag("BELOW_MATCH_THRESHOLD", score=0.2)
    lines = [r.getMessage() for r in caplog.records if "face_diag" in r.getMessage()]
    assert len(lines) == 2 and "camera=7" in lines[0] and "reason=NO_EMBEDDING" in lines[0]
    assert p.diag_snapshot() == {"NO_EMBEDDING": 3, "BELOW_MATCH_THRESHOLD": 1}


# --- retry of faces InsightFace's detector misses in their crop ---------------

def _retry_pipeline(monkeypatch, embed_calls):
    """Pipeline whose _embed finds a face only in a bordered crop (the plain crop is 108 px, bordered 216)."""
    live = _unit(5)
    p = _pipeline([_entry("13", live)], None)
    p.frame_idx = 10
    p._refresh_gallery = lambda: None

    def fake_embed(crop):
        embed_calls.append(crop.shape[:2])
        return live if crop.shape[0] > 150 else None

    p._embed = fake_embed
    monkeypatch.setattr(face_pipeline.analytics_settings, "enabled", lambda name: False)
    monkeypatch.setattr(face_pipeline.attendance, "record", lambda *a, **k: None)
    monkeypatch.setattr(face_pipeline, "FACE_EMBED_RETRY", True)
    return p


def _face_row():
    return np.array([100, 100, 160, 160, 0.9], dtype=np.float32)   # 60x60 face


def test_failed_embedding_is_retried_once_with_a_border_and_can_name_the_person(monkeypatch):
    calls = []
    p = _retry_pipeline(monkeypatch, calls)
    pstate = face_pipeline.PersonTrackState(track_id=1)
    frame = np.zeros((400, 400, 3), dtype=np.uint8)
    p._update_person_identity(pstate, frame, _face_row())
    assert len(calls) == 2 and calls[1][0] > calls[0][0]       # second try on a bigger (bordered) crop
    assert pstate.pending_identity == "13"                       # and it produced a name vote
    assert p.diag_snapshot() == {}                               # not counted as a failure


def test_retry_is_throttled_per_person_and_failures_are_counted(monkeypatch):
    calls = []
    p = _retry_pipeline(monkeypatch, calls)
    p._embed = lambda crop: (calls.append(crop.shape[:2]), None)[1]    # never finds a face
    pstate = face_pipeline.PersonTrackState(track_id=1)
    frame = np.zeros((400, 400, 3), dtype=np.uint8)
    p._update_person_identity(pstate, frame, _face_row())
    first = len(calls)
    p.frame_idx += 5
    p._update_person_identity(pstate, frame, _face_row())          # inside the throttle window
    assert first == 2 and len(calls) == 3                          # plain try only, no second retry
    assert p.diag_snapshot() == {"NO_EMBEDDING": 2}                # a 60x60 face is big enough, so not FACE_TOO_SMALL


def test_a_tiny_face_that_yields_no_embedding_is_reported_as_too_small(monkeypatch):
    p = _retry_pipeline(monkeypatch, [])
    p._embed = lambda crop: None
    tiny = np.array([100, 100, 120, 120, 0.9], dtype=np.float32)    # 20x20 = 400 px < FACE_MIN_AREA
    p._update_person_identity(face_pipeline.PersonTrackState(track_id=1), np.zeros((400, 400, 3), np.uint8), tiny)
    assert p.diag_snapshot() == {"FACE_TOO_SMALL": 1}


def test_retry_can_be_switched_off(monkeypatch):
    calls = []
    p = _retry_pipeline(monkeypatch, calls)
    monkeypatch.setattr(face_pipeline, "FACE_EMBED_RETRY", False)
    p._update_person_identity(face_pipeline.PersonTrackState(track_id=1), np.zeros((400, 400, 3), np.uint8), _face_row())
    assert len(calls) == 1


# --- weak person detections must still become boxes ---------------------------

def test_person_detections_the_detector_accepts_are_all_trackable():
    import supervision as sv

    scores = CameraFacePipeline._person_tracker_scores(np.array([face_pipeline.PERSON_DETECT_CONF, 0.35, 0.45, 0.6, 1.0]))
    assert np.all(np.diff(scores) > 0)                      # order preserved
    assert scores.min() >= 0.6 and scores.max() <= 1.0
    for raw in (0.31, 0.35, 0.45, 0.8):
        tracker = sv.ByteTrack(track_activation_threshold=0.4, lost_track_buffer=8,
                               minimum_matching_threshold=face_pipeline.PERSON_TRACKER_MATCH_THRESHOLD)
        shown = []
        for k in range(6):
            det = sv.Detections(xyxy=np.array([[100 + k, 100, 220 + k, 380]], dtype=np.float32),
                                confidence=CameraFacePipeline._person_tracker_scores(np.array([raw])))
            shown.append(len(tracker.update_with_detections(det)))
        assert shown == [1] * 6, (raw, shown)               # steady, not flickering


def test_raw_weak_scores_would_have_been_dropped_by_the_tracker():
    """The behaviour the remap exists to avoid (documents the library's rule)."""
    import supervision as sv

    tracker = sv.ByteTrack(track_activation_threshold=0.4, lost_track_buffer=8, minimum_matching_threshold=0.6)
    det = sv.Detections(xyxy=np.array([[100, 100, 220, 380]], dtype=np.float32), confidence=np.array([0.45], dtype=np.float32))
    assert len(tracker.update_with_detections(det)) == 0


# --- naming speed: one strong read is enough, weak ones still need two -------------

def _near(live, cos, seed=9):
    other = _unit(seed)
    o = other - np.dot(live, other) * live
    o /= np.linalg.norm(o)
    return (cos * live + np.sqrt(1 - cos * cos) * o).astype(np.float32)


def test_a_strong_unambiguous_gallery_match_names_the_person_on_the_first_read(monkeypatch):
    live = _unit(5)
    p = _retry_pipeline(monkeypatch, [])
    p._gallery_index = fm.build_index([_entry("13", _near(live, 0.8)), _entry("2", _near(live, 0.1, seed=11))])
    p._embed = lambda crop: live
    st = face_pipeline.PersonTrackState(track_id=1)
    p._update_person_identity(st, np.zeros((400, 400, 3), np.uint8), _face_row())
    assert st.current_identity == "13"


def test_a_borderline_gallery_match_still_needs_two_agreeing_reads(monkeypatch):
    live = _unit(5)
    p = _retry_pipeline(monkeypatch, [])
    p._gallery_index = fm.build_index([_entry("13", _near(live, 0.42))])      # above 0.35, below 0.5
    p._embed = lambda crop: live
    st = face_pipeline.PersonTrackState(track_id=1)
    frame = np.zeros((400, 400, 3), np.uint8)
    p._update_person_identity(st, frame, _face_row())
    assert st.current_identity is None and st.pending_identity == "13"
    p.frame_idx += 5
    p._update_person_identity(st, frame, _face_row())
    assert st.current_identity == "13"


def test_two_people_close_together_in_score_are_not_named_on_one_read(monkeypatch):
    live = _unit(5)
    p = _retry_pipeline(monkeypatch, [])
    p._gallery_index = fm.build_index([_entry("13", _near(live, 0.62)), _entry("2", _near(live, 0.52, seed=11))])
    p._embed = lambda crop: live
    st = face_pipeline.PersonTrackState(track_id=1)
    p._update_person_identity(st, np.zeros((400, 400, 3), np.uint8), _face_row())
    assert st.current_identity is None                                      # lead 0.10 < 0.15: vote needed


def test_face_crops_are_re_detected_at_the_smaller_detector_size():
    seen = {}

    class Det:
        def detect(self, img, input_size=None, max_num=0, metric="default"):
            seen["size"] = input_size
            return np.array([[10, 10, 90, 90, 0.9]], np.float32), np.zeros((1, 5, 2), np.float32)

    class Rec:
        def get(self, img, face):
            face.embedding = np.full(512, 2.0, np.float32)
            return face.embedding

    p = object.__new__(CameraFacePipeline)
    p._arcface = type("A", (), {"det_model": Det(), "models": {"recognition": Rec()}})()
    emb = CameraFacePipeline._embed(p, np.zeros((110, 110, 3), np.uint8))
    assert seen["size"] == (face_pipeline.FACE_EMBED_DET_SIZE,) * 2 and face_pipeline.FACE_EMBED_DET_SIZE < 640
    assert abs(float(np.linalg.norm(emb)) - 1.0) < 1e-5


def test_a_photo_match_cannot_override_the_classifier_for_a_person_it_was_trained_on():
    """Live bug: Pushpendra (classifier: Pushpendra) was shown as Ravi because a
    weaker enrolled-photo match to Ravi overrode the classifier."""
    live = _unit(5)
    p = _pipeline([_entry("8", _near(live, 0.6))], _Clf("10", 0.9, others=("8", "2")))
    assert p._identify_for_overlay(live) == ("10", 0.9)
