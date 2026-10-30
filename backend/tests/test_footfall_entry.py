"""Footfall at a gate with an ENTRY LINE: only people who cross it from outside
to inside are counted (and get a clip and a name). People walking past or
standing outside, or first seen already inside, or stepping over and straight
back, are never counted.

Drives the real FootfallService, Re-ID gallery/fusion and Staff Count's line
tracker (reused for the crossing). Only detection and the body embedder are
faked: each person walks along their own column of the picture, which is how
the fake embedder knows who a box is.
"""

import numpy as np
import pytest

from app import face_pipeline, footfall
from app.reid import reid_db
from tests.test_footfall_gates import GATE_1, GATE_3, another_view, person_embedding, svc  # noqa: F401 (svc is a fixture)

# A tall frame so a person can be fully above or fully below the line.
FRAME = np.random.RandomState(1).randint(60, 200, (800, 520, 3)).astype(np.uint8)
LINE = [[0.0, 0.5], [1.0, 0.5]]      # across the picture at y = 400 px
INSIDE = 1                            # the side below the line is inside
OUT_Y, IN_Y = 300, 520                # foot positions clearly outside / inside


class ColumnEmbedder:
    """Person p is always drawn at x = 100 * p; another view each time."""

    def __init__(self):
        self.n = 0

    def embed(self, frame, bbox):
        self.n += 1
        return another_view(person_embedding(round(bbox[0] / 100)), 20_000 + self.n)


def box(person, foot_y):
    x = 100 * person
    return [float(x), float(foot_y - 280), float(x + 110), float(foot_y)]


@pytest.fixture
def gate(svc, monkeypatch):
    monkeypatch.setattr(face_pipeline, "get_existing_pipeline", lambda cid: None)
    for cid in (GATE_1, GATE_3):
        reid_db.set_entry_line(cid, LINE, INSIDE)
        runner = footfall._GateRunner(cid, svc._gallery, svc._generation)
        runner.state.embedder = ColumnEmbedder()
        svc._runners[cid] = runner
    svc.clock = __import__("time").time() - 300   # today, so the summary's "today" sees it
    return svc


def look(svc, people, gate_id=GATE_1, dt=0.5):
    """people: [(track_id, person, foot_y)]"""
    svc.clock += dt
    svc._process(svc._runners[gate_id], FRAME,
                 [{"track_id": t, "bbox": box(p, y), "confidence": 0.9} for t, p, y in people], svc.clock)


def walk(svc, track, person, ys, gate_id=GATE_1, looks_each=1):
    for y in ys:
        for _ in range(looks_each):
            look(svc, [(track, person, y)], gate_id)


def counted():
    with reid_db.get_connection() as conn:
        return conn.execute("SELECT COUNT(DISTINCT person_id) FROM reid_events").fetchone()[0]


def test_walking_in_through_the_gate_is_counted_once_with_a_clip(gate):
    walk(gate, 1, 1, [OUT_Y] * 6)                      # approaching, outside
    assert counted() == 0
    walk(gate, 1, 1, [IN_Y] * 6)                       # crossed in and stayed in
    assert counted() == 1
    assert len(gate.clip_requests) == 1
    _cam, t0, _final, _done = gate.clip_requests[0]
    assert gate.clock - 3.5 <= t0 <= gate.clock        # the clip is centred on the crossing


def test_standing_outside_is_never_counted(gate):
    walk(gate, 1, 1, [OUT_Y] * 60)                     # 30 s just outside the gate
    assert counted() == 0 and gate.clip_requests == []
    assert reid_db.count_unique_lifetime() == 1        # Re-ID did learn them (for when they do enter)
    assert gate.summary()["unique_today"] == 0 and gate.summary()["new_today"] == 0


def test_walking_past_outside_is_never_counted(gate):
    for x_person in (1, 2, 3):                         # three people pass along outside
        walk(gate, 10 + x_person, x_person, [OUT_Y - 40, OUT_Y, OUT_Y - 20, OUT_Y] * 3)
    assert counted() == 0 and gate.clip_requests == []


def test_first_seen_already_inside_is_not_an_entry(gate):
    walk(gate, 1, 1, [IN_Y] * 20)
    assert counted() == 0


def test_stepping_over_and_straight_back_is_not_an_entry(gate):
    walk(gate, 1, 1, [OUT_Y] * 6)
    walk(gate, 1, 1, [IN_Y])                           # one look over the line...
    walk(gate, 1, 1, [OUT_Y] * 10)                     # ...and straight back out
    assert counted() == 0


def test_walking_out_is_not_an_entry(gate):
    walk(gate, 1, 1, [IN_Y] * 6)
    walk(gate, 1, 1, [OUT_Y] * 6)
    assert counted() == 0


def test_same_person_entering_twice_is_one_count_and_one_clip(gate):
    walk(gate, 1, 1, [OUT_Y] * 6 + [IN_Y] * 6)
    walk(gate, 1, 1, [OUT_Y] * 6 + [IN_Y] * 6)        # out again and back in
    walk(gate, 7, 1, [OUT_Y] * 6 + [IN_Y] * 6, gate_id=GATE_3)   # and in through another gate
    assert counted() == 1 and len(gate.clip_requests) == 1


def test_two_people_entering_are_two(gate):
    for _ in range(6):
        look(gate, [(1, 1, OUT_Y), (2, 3, OUT_Y)])
    for _ in range(6):
        look(gate, [(1, 1, IN_Y), (2, 3, IN_Y)])
    assert counted() == 2 and len(gate.clip_requests) == 2


def test_vanishing_through_the_door_right_after_crossing_counts(gate):
    walk(gate, 1, 1, [OUT_Y] * 6)
    walk(gate, 1, 1, [IN_Y])                           # crossed, then out of view
    for _ in range(20):                                # the gate keeps looking, nobody there
        look(gate, [])
    assert counted() == 1


def test_an_employee_is_named_from_their_face_while_approaching(gate, monkeypatch):
    from app import employee_directory

    class Pipe:
        calls = 0

        def _identify_head(self, frame, head):
            Pipe.calls += 1
            return "EMP-1", 0.95, "match"

    monkeypatch.setattr(face_pipeline, "get_existing_pipeline", lambda cid: Pipe())
    monkeypatch.setattr(employee_directory, "get_display", lambda eid: ({"EMP-1": "Asha Rao"}.get(eid), "#00f"))
    walk(gate, 1, 1, [OUT_Y] * 6)
    if gate._identify_future is not None:
        gate._identify_future.result(timeout=10)
    assert counted() == 0                              # identified, but not counted: not in yet
    walk(gate, 1, 1, [IN_Y] * 6)
    v = gate.summary()["visitors"]
    assert len(v) == 1 and v[0]["name"] == "Asha Rao"


def test_people_outside_are_never_named_or_listed(gate):
    walk(gate, 1, 1, [OUT_Y] * 20)
    assert gate.summary()["visitors"] == []


def test_a_gate_without_a_line_still_counts_presence_in_its_area(svc):
    # Unchanged behaviour until a line is drawn (covered in detail by test_footfall_gates).
    assert reid_db.get_camera_config(GATE_1)["entry_line"] is None
    assert svc._runners[GATE_1].crosser is None
