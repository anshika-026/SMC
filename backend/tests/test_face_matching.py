"""Gallery matching: per-person best score, second-best ambiguity, duplicate
and conflicting references, and the rejection reasons (app/face_matching.py)."""

import numpy as np

from app import face_matching as fm

DIM = 512


def _unit(seed: int) -> np.ndarray:
    v = np.random.default_rng(seed).normal(size=DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _blend(a: np.ndarray, b: np.ndarray, w: float) -> np.ndarray:
    """Unit vector whose cosine to `a` is ~w (b is made orthogonal to a first)."""
    b = b - np.dot(a, b) * a
    b /= np.linalg.norm(b)
    return w * a + np.sqrt(1 - w * w) * b


def _entry(pid: str, vec) -> dict:
    return {"person_id": pid, "embedding": list(map(float, vec))}


def test_confirmed_match_uses_best_single_reference_not_the_average():
    front, side = _unit(1), _unit(2)               # two very different poses of person A
    live = _blend(side, _unit(3), 0.8)             # live frame resembles the side pose only
    idx = fm.build_index([_entry("A", front), _entry("A", side), _entry("B", _unit(4))])
    res = fm.match(idx, live, threshold=0.5)
    assert res.person_id == "A" and res.reason is None
    assert res.score > 0.75 and res.n_refs == 2


def test_below_threshold_is_rejected_with_candidate_kept_for_diagnostics():
    idx = fm.build_index([_entry("A", _unit(1)), _entry("B", _unit(2))])
    res = fm.match(idx, _unit(99), threshold=0.45)
    assert res.person_id is None and res.reason == fm.BELOW_MATCH_THRESHOLD
    assert res.best_person_id in ("A", "B") and res.second_person_id in ("A", "B")


def test_two_people_about_equally_close_is_ambiguous_not_first_wins():
    a, b = _unit(1), _unit(2)
    live = _unit(5)
    ra = _blend(live, a, 0.62)
    rb = _blend(live, b, 0.60)
    idx = fm.build_index([_entry("A", ra), _entry("B", rb)])
    res = fm.match(idx, live, threshold=0.45, margin=0.05)
    assert res.person_id is None and res.reason == fm.AMBIGUOUS_MATCH
    assert res.margin < 0.05


def test_clear_winner_over_a_weak_second_is_accepted():
    live = _unit(5)
    idx = fm.build_index([_entry("A", _blend(live, _unit(1), 0.70)),
                          _entry("B", _blend(live, _unit(2), 0.20))])
    res = fm.match(idx, live, threshold=0.45)
    assert res.person_id == "A" and res.reason is None


def test_weak_second_below_threshold_never_causes_ambiguity():
    live = _unit(5)
    idx = fm.build_index([_entry("A", _blend(live, _unit(1), 0.50)),
                          _entry("B", _blend(live, _unit(2), 0.44))])
    # margin 0.06 > 0.05 anyway, but B is also under the threshold: A wins.
    assert fm.match(idx, live, threshold=0.45, margin=0.5).person_id == "A"


def test_same_photo_enrolled_twice_counts_as_one_reference():
    v = _unit(1)
    idx = fm.build_index([_entry("A", v), _entry("A", v), _entry("A", v * 1.0), _entry("B", _unit(2))])
    assert idx.refs_for("A") == 1
    assert idx.duplicates_dropped == 2
    assert fm.match(idx, v, threshold=0.45).n_refs == 1


def test_same_reference_filed_under_two_people_is_excluded_for_both_and_reported():
    shared = _unit(1)
    idx = fm.build_index([_entry("A", shared), _entry("B", shared),
                          _entry("A", _unit(2)), _entry("C", _unit(3))])
    assert [(a, b) for a, b, _ in idx.conflicts] == [("A", "B")]
    assert idx.refs_for("B") == 0 and idx.refs_for("A") == 1
    # The shared face is no longer silently given to A (or B).
    res = fm.match(idx, shared, threshold=0.45)
    assert res.person_id is None


def test_empty_gallery_reason():
    res = fm.match(fm.build_index([]), _unit(1), threshold=0.45)
    assert res.person_id is None and res.reason == fm.NO_ENROLLED_REFERENCES


def test_invalid_live_embeddings_are_rejected():
    idx = fm.build_index([_entry("A", _unit(1))])
    for bad in (None, [], [float("nan")] * DIM, [0.0] * DIM, [1.0] * 100, np.ones(DIM) * 5):
        assert fm.match(idx, bad, threshold=0.45).reason == fm.INVALID_EMBEDDING


def test_invalid_gallery_rows_are_skipped_not_fatal():
    idx = fm.build_index([_entry("A", _unit(1)), {"person_id": "B", "embedding": [float("nan")] * DIM},
                          {"person_id": "C", "embedding": [1.0] * 10}, {"person_id": "D", "embedding": None}])
    assert idx.invalid_dropped == 3 and len(idx) == 1


def test_single_enrolled_person_has_no_second_best():
    idx = fm.build_index([_entry("A", _unit(1))])
    res = fm.match(idx, _unit(1), threshold=0.45)
    assert res.person_id == "A" and res.second_person_id is None
