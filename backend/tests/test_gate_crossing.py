"""Entry gates mark only people who come IN (gate_crossing.py and its use in intrusion.py)."""

import time

import numpy as np
import pytest

from app import alerts, analytics_settings, camera_db, evidence, intrusion
from app.gate_crossing import GateCounter
from tests.conftest import bearer

W, H = 1000, 500
# A vertical gate line in the middle of the picture. With inside_sign 1 the LEFT side is inside
# (the same convention the editor's "Inside" arrow uses).
LINE = [[0.5, 0.1], [0.5, 0.9]]
AREA = [[0.05, 0.05], [0.95, 0.05], [0.95, 0.95], [0.05, 0.95]]


def at(x, tid=1, y=400):
    return {"track_id": tid, "confidence": 0.9, "bbox": [x - 30, y - 200, x + 30, y]}


def walk(gc, xs, t0=0.0, step=0.5, tid=1, zone=1, sign=1):
    out = []
    for i, x in enumerate(xs):
        out += gc.observe(zone, LINE, sign, [at(x, tid)], W, H, t0 + i * step, area=AREA)
    return out


def test_walking_in_is_marked_once():
    gc = GateCounter()
    assert len(walk(gc, [800, 700, 600, 400, 300, 250, 200, 200])) == 1


def test_walking_out_is_never_marked():
    assert walk(GateCounter(), [200, 300, 400, 600, 700, 800, 800]) == []


def test_inside_sign_flips_which_way_is_in():
    assert len(walk(GateCounter(), [200, 300, 400, 600, 700, 800, 800], sign=-1)) == 1


def test_first_seen_inside_or_standing_on_the_line_is_not_an_entry():
    assert walk(GateCounter(), [200, 210, 190, 200, 205]) == []          # already inside
    assert walk(GateCounter(), [800, 505, 495, 510, 490, 500]) == []      # dithering on the line


def test_a_quick_step_back_before_confirming_is_not_an_entry():
    assert walk(GateCounter(), [800, 400, 800, 800, 800], step=0.3) == []


def test_walking_in_and_out_of_view_counts():
    gc = GateCounter()
    assert walk(gc, [800, 700, 300]) == []                               # just crossed, not confirmed yet
    assert len(gc.observe(1, LINE, 1, [], W, H, 4.0, area=AREA)) == 1    # then left the picture


def test_each_person_is_marked_and_a_round_trip_marks_once():
    gc = GateCounter()
    entries = []
    for k, x in enumerate([800, 600, 300, 250, 200, 200]):
        entries += gc.observe(1, LINE, 1, [at(x, 1), at(x + 20, 2)], W, H, k * 0.6, area=AREA)
    assert sorted(e["track_id"] for e in entries) == [1, 2]                  # two people, two entries
    gc2 = GateCounter()
    entries = walk(gc2, [800, 300, 250, 200, 600, 700, 800, 800, 300, 250, 200, 200])   # in, out, in again
    assert len(entries) == 2


def test_outside_the_gate_area_or_past_the_line_ends_does_not_count():
    small = [[0.4, 0.2], [0.6, 0.2], [0.6, 0.6], [0.4, 0.6]]
    gc = GateCounter()
    assert [e for i, x in enumerate([800, 600, 300, 200, 200]) for e in gc.observe(1, LINE, 1, [at(x)], W, H, i * 0.5, area=small)] == []
    far = GateCounter()
    assert [e for i, x in enumerate([800, 600, 300, 200, 200]) for e in far.observe(1, [[0.5, 0.1], [0.5, 0.3]], 1, [at(x, y=495)], W, H, i * 0.5)] == []


# ---- through the intrusion engine --------------------------------------------------------

class FakeEvidence:
    def __init__(self):
        self.scheduled = []

    def schedule(self, alert_id, camera_id, event_ts):
        self.scheduled.append(alert_id)


@pytest.fixture
def gate(api, admin_token, monkeypatch):
    intrusion.init_db()
    alerts.init_db()
    monkeypatch.setattr(alerts, "SNAPSHOT_DIR", alerts.SNAPSHOT_DIR.parent / "snaps-gate-test")
    monkeypatch.setattr(analytics_settings, "_state", {k: True for k in analytics_settings.FEATURES})
    svc = intrusion.IntrusionService()
    monkeypatch.setattr(intrusion, "service", svc)
    fake = FakeEvidence()
    monkeypatch.setattr(evidence, "service", fake)
    cam = camera_db.add_camera("Reception", "HQ")
    h = bearer(admin_token)
    r = api.post("/api/intrusion/zones", headers=h, json={
        "camera_id": cam, "name": "Reception Entry Gate", "area": "Reception", "is_entry_gate": True,
        "polygon": AREA, "entry_line": LINE, "inside_sign": 1})
    assert r.status_code == 200, r.text
    return api, h, cam, svc, fake, r.json()


def run(svc, cam, people_per_step, t0=None, step=0.5):
    frame = np.zeros((H, W, 3), np.uint8)
    t0 = t0 or time.time()
    for i, people in enumerate(people_per_step):
        zones = [z for z in intrusion.list_zones(cam) if z["enabled"]]
        svc._detect(cam, frame, zones, t0 + i * step, [p["bbox"] for p in people], people)


def test_each_person_coming_in_gets_their_own_alert_and_clip(gate):
    api, h, cam, svc, fake, zone = gate
    assert zone["entry_line"] == LINE and zone["inside_sign"] == 1
    run(svc, cam, [[at(800, 1)], [at(600, 1)], [at(300, 1)], [at(250, 1)], [at(200, 1)], [at(200, 1)]])
    run(svc, cam, [[at(820, 2)], [at(650, 2)], [at(300, 2)], [at(260, 2)], [at(220, 2)], [at(220, 2)]], t0=time.time() + 10)
    rows = alerts.list_alerts("all")
    assert len(rows) == 2 and all(r["message"] == "1 person came in through Reception Entry Gate" for r in rows)
    assert len(fake.scheduled) == 2 and intrusion.stats()["today"] == 2


def test_people_going_out_are_not_marked(gate):
    api, h, cam, svc, fake, zone = gate
    run(svc, cam, [[at(200, 1)], [at(300, 1)], [at(600, 1)], [at(700, 1)], [at(800, 1)], [at(800, 1)]])
    run(svc, cam, [[at(250, 3)], [at(250, 3)], [at(240, 3)]])                    # standing inside
    assert alerts.list_alerts("all") == [] and fake.scheduled == []


def test_gate_line_validation_and_unchanged_area_zones(gate):
    api, h, cam, svc, fake, zone = gate
    bad = api.post("/api/intrusion/zones", headers=h, json={"camera_id": cam, "name": "X", "is_entry_gate": True,
                                                           "polygon": AREA, "entry_line": [[0.5, 0.5], [0.51, 0.5]]})
    assert bad.status_code == 422
    nogate = api.post("/api/intrusion/zones", headers=h, json={"camera_id": cam, "name": "Y", "polygon": AREA, "entry_line": LINE})
    assert nogate.status_code == 422                                              # a line only on entry gates
    flipped = api.patch(f"/api/intrusion/zones/{zone['id']}", headers=h, json={"inside_sign": -1}).json()
    assert flipped["inside_sign"] == -1
    # a restricted area (no gate line) still alerts on presence, as in MAINDV
    room = api.post("/api/intrusion/zones", headers=h, json={"camera_id": cam, "name": "Server room",
                                                            "polygon": [[0.05, 0.05], [0.45, 0.05], [0.45, 0.95], [0.05, 0.95]]}).json()
    run(svc, cam, [[at(200, 5)]])
    assert [r["zone_name"] for r in alerts.list_alerts("all")] == [room["name"]]


def test_first_seen_past_the_line_then_walking_further_in_counts():
    """The door frame hides someone as they come through, so they are first seen already past the
    line; walking further in from there is coming in. Going back out the same way is not."""
    assert len(walk(GateCounter(), [480, 450, 420, 380, 350])) == 1     # appeared at the line, walked in
    assert walk(GateCounter(), [350, 380, 420, 460, 490]) == []          # walked from inside towards the door
    assert walk(GateCounter(), [300, 290, 300, 295]) == []              # sitting/standing inside
    assert walk(GateCounter(), [380, 340, 300, 260]) == []              # already well inside, walking across
