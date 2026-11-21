"""Visitor seating-area alerts: employee exclusion, one alert per visit and a
repeat alert every 30 minutes, with a 10-second detection gap (visitors.py).

Drives VisitorService.process() directly with simulated looks (no camera, no
models). The face pipeline's identity is faked through VisitorService.identify,
which in production only reads that pipeline's live tracks.
"""

import pytest

from app import visitors

W = H = 1000
AREA = [[0, 0], [1, 0], [1, 1], [0, 1]]
MIN = 60.0
T0 = 1_800_000_000.0  # any fixed start time


def seat(x):
    """A seated person's box; x picks the seat, so two people never overlap."""
    return [x, 400, x + 150, 800]


class World:
    def __init__(self, svc):
        self.svc = svc
        self.now = T0
        self.identity = {}   # seat x -> employee id the face pipeline reports
        svc.identify = lambda cam, bbox, w, h: self.identity.get(bbox[0])

    def look(self, people, ts=None, camera_id=1):
        """people: {track_id: seat x}"""
        if ts is not None:
            self.now = ts
        self.svc.process(camera_id, AREA, W, H, [{"track_id": t, "bbox": seat(x), "confidence": 0.9} for t, x in people.items()], self.now)

    def run(self, seconds, people, step=1.0, camera_id=1):
        end = self.now + seconds
        while self.now < end:
            self.look(people, camera_id=camera_id)
            self.now += step

    def alerts(self):
        return sorted(visitors.list_events(limit=200), key=lambda e: e["id"])


@pytest.fixture
def world(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    monkeypatch.setattr(visitors, "CLIPS_DIR", tmp_path / "visitor_clips")
    # Policy under test (the defaults, pinned so an .env can't change them).
    monkeypatch.setattr(visitors, "ENTRY_CONFIRM_LOOKS", 2)
    monkeypatch.setattr(visitors, "DETECTION_GAP_SECONDS", 10.0)
    monkeypatch.setattr(visitors, "REPEAT_ALERT_MINUTES", 30.0)
    monkeypatch.setattr(visitors, "PRESENCE_BREAK_SECONDS", 3.0)
    monkeypatch.setattr(visitors, "ABSENCE_GRACE_SECONDS", 60.0)
    monkeypatch.setattr(visitors, "EXCLUDE_EMPLOYEES", True)
    visitors.init_db()
    svc = visitors.VisitorService()
    w = World(svc)
    w.clips = []
    svc._schedule_clip = lambda event_id, camera_id, t0: w.clips.append(event_id)  # no video in tests
    return w


def test_1_enrolled_employee_never_alerts(world):
    world.identity[100] = "EMP-7"
    world.run(45 * MIN, {1: 100})
    assert world.alerts() == []
    st = [v.kind for v in world.svc._open[1].values()]
    assert st == ["employee"]


def test_2_unknown_person_one_alert_after_the_detection_gap(world):
    world.run(9, {1: 100})
    assert world.alerts() == []                     # not before 10 s in the area
    world.run(3, {1: 100})
    a = world.alerts()
    assert len(a) == 1 and a[0]["alert_seq"] == 1 and a[0]["status"] == "inside"
    assert 10 <= a[0]["alert_at"] - T0 <= 12


def test_3_no_repeat_before_30_minutes(world):
    world.run(29 * MIN, {1: 100})
    assert len(world.alerts()) == 1


def test_4_second_alert_only_after_the_gap_past_30_minutes(world):
    world.run(12, {1: 100})
    first = world.alerts()[0]["alert_at"]
    due = first + 30 * MIN
    world.run(due - world.now + 5, {1: 100})        # 5 s past due: still waiting for the gap
    assert len(world.alerts()) == 1
    world.run(6, {1: 100})                          # 10 s past due: second alert
    a = world.alerts()
    assert [e["alert_seq"] for e in a] == [1, 2]
    assert 10 <= a[1]["alert_at"] - due <= 12
    assert a[0]["entered_at"] == a[1]["entered_at"]  # same visit, same arrival time


def test_5_repeats_every_30_minutes_not_every_10_seconds(world):
    world.run(125 * MIN, {1: 100})
    a = world.alerts()
    assert len(a) == 5                              # ~0:10, 30:20, 60:30, 90:40, 120:50
    # Each repeat is due 30 min after the previous alert, then waits the 10 s gap.
    gaps = [b["alert_at"] - x["alert_at"] for x, b in zip(a, a[1:])]
    assert all(30 * MIN + 10 <= g <= 30 * MIN + 12 for g in gaps)


def test_6_brief_loss_and_new_track_id_neither_alert_nor_reset_the_timer(world):
    world.run(12, {1: 100})
    first = world.alerts()[0]["alert_at"]
    world.run(10 * MIN, {1: 100})
    world.run(20, {})                               # lost for 20 s (occlusion)
    world.run(5, {2: 100})                          # back with a NEW tracker id
    assert len(world.alerts()) == 1
    world.run(first + 30 * MIN + 12 - world.now, {2: 100})
    a = world.alerts()
    assert len(a) == 2 and a[1]["alert_at"] - first <= 30 * MIN + 12  # timer kept, not restarted
    assert len(world.svc._open[1]) == 1


def test_6b_loss_at_due_time_waits_for_presence_again(world):
    world.run(12, {1: 100})
    first = world.alerts()[0]["alert_at"]
    world.run(first + 30 * MIN - 5 - world.now, {1: 100})
    world.run(30, {})                               # not seen over the due time
    assert len(world.alerts()) == 1
    back = world.now
    world.run(12, {1: 100})
    a = world.alerts()
    assert len(a) == 2 and 10 <= a[1]["alert_at"] - back <= 12


def test_7_leaving_and_returning_after_the_visit_closed_is_a_new_visit(world):
    world.run(12, {1: 100})
    world.run(70, {})                               # gone longer than the absence grace
    old = world.alerts()
    assert len(old) == 1 and old[0]["status"] == "left"
    back = world.now
    world.run(12, {3: 100})
    a = world.alerts()
    assert len(a) == 2 and a[1]["alert_seq"] == 1   # a new cycle
    assert a[1]["entered_at"] >= back


def test_8_two_people_have_independent_schedules(world):
    world.run(5 * MIN, {1: 100})                    # A sits at 0:00
    world.run(30 * MIN, {1: 100, 2: 500})           # B sits at 5:00
    a = world.alerts()
    by_track = {}
    for e in a:
        by_track.setdefault(e["track_id"], []).append(e["alert_at"] - T0)
    assert len(by_track[1]) == 2 and len(by_track[2]) == 1
    assert by_track[2][0] >= 5 * MIN + 10


def test_9_late_recognition_prevents_a_false_alert(world):
    world.run(6, {1: 100})                          # face not recognised yet
    world.identity[100] = "EMP-3"                   # recognised within the gap
    world.run(5 * MIN, {1: 100})
    assert world.alerts() == []


def test_9b_a_missed_recognition_frame_does_not_turn_an_employee_into_a_visitor(world):
    world.identity[100] = "EMP-3"
    world.run(3, {1: 100})
    del world.identity[100]                         # recognition drops out afterwards
    world.run(40 * MIN, {1: 100})
    assert world.alerts() == []


def test_9c_recognition_after_an_alert_removes_that_alert(world):
    world.run(12, {1: 100})
    assert len(world.alerts()) == 1
    world.identity[100] = "EMP-3"
    world.run(2, {1: 100})
    assert world.alerts() == []
    world.run(40 * MIN, {1: 100})
    assert world.alerts() == []


def test_employee_seat_is_not_inherited_by_someone_else_later(world):
    world.identity[100] = "EMP-3"
    world.run(60, {1: 100})
    world.run(20, {})                               # the employee gets up
    del world.identity[100]
    world.run(12, {9: 100})                         # a stranger takes the same seat
    assert len(world.alerts()) == 1


def test_without_face_recognition_on_the_camera_everyone_alerts(world, monkeypatch):
    monkeypatch.setattr(visitors, "EXCLUDE_EMPLOYEES", False)
    world.identity[100] = "EMP-3"
    world.run(12, {1: 100})
    assert len(world.alerts()) == 1


def test_counts_exclude_employees(world, monkeypatch):
    world.identity[100] = "EMP-3"
    world.run(15, {1: 100, 2: 500})
    from app import camera_stream

    class _S:
        state, last_frame_at = "running", None

    monkeypatch.setattr(camera_stream, "get_stream", lambda cid: _S())
    st = world.svc.camera_status(1)
    assert st["inside_now"] == 1 and st["employees_now"] == 1 and st["unverified_now"] == 0


def test_identify_reads_the_face_pipeline_tracks(world, monkeypatch):
    from app import face_pipeline

    class _Pipe:
        frame_size = (2000, 2000)   # its own frame size: boxes compared as fractions

        def get_live_detections(self):
            return [
                {"bbox": [200, 800, 500, 1600], "employee_id": "EMP-1", "identity_source": "face"},
                {"bbox": [1000, 800, 1300, 1600], "employee_id": None, "identity_source": None},
            ]

    monkeypatch.setattr(face_pipeline, "get_existing_pipeline", lambda cid: _Pipe())
    svc = visitors.VisitorService()
    assert svc.identify(1, seat(100), W, H) == "EMP-1"
    assert svc.identify(1, seat(500), W, H) is None
    monkeypatch.setattr(visitors, "EMPLOYEE_SOURCES", frozenset({"appearance"}))
    assert svc.identify(1, seat(100), W, H) is None
    monkeypatch.setattr(face_pipeline, "get_existing_pipeline", lambda cid: None)
    assert svc.identify(1, seat(100), W, H) is None


# ---- employee records, visit history, clips --------------------------------

def test_employee_is_recorded_apart_from_visitors(world):
    world.identity[100] = "EMP-7"
    world.run(5 * MIN, {1: 100})
    assert world.alerts() == [] and world.clips == [] and visitors.list_visits() == []
    emp = visitors.list_employee_visits()
    assert len(emp) == 1 and emp[0]["employee_id"] == "EMP-7" and emp[0]["status"] == "inside"
    assert emp[0]["last_seen"] - emp[0]["arrived_at"] >= 4 * MIN
    world.run(70, {})                               # leaves
    emp = visitors.list_employee_visits()
    assert len(emp) == 1 and emp[0]["status"] == "left"


def test_one_visit_is_one_history_row_with_its_alerts(world):
    world.run(65 * MIN, {1: 100})                   # alerts at ~0:10, 30:20, 60:30
    v = visitors.list_visits()
    assert len(v) == 1 and v[0]["alerts"] == 3 and v[0]["status"] == "inside"
    assert v[0]["last_seen"] - v[0]["arrived_at"] >= 64 * MIN
    assert v[0]["latest_event_id"] == world.alerts()[-1]["id"]
    world.run(70, {})
    v = visitors.list_visits()
    assert len(v) == 1 and v[0]["status"] == "left" and v[0]["left_at"] is not None


def test_one_clip_per_alert_never_per_detection(world):
    world.run(29 * MIN, {1: 100})
    assert len(world.clips) == 1                    # seated 29 min: one clip
    world.run(2 * MIN, {1: 100})
    assert len(world.clips) == 2 == len(world.alerts())   # next allowed alert, its own clip


def test_leaving_and_returning_makes_two_visits(world):
    world.run(12, {1: 100})
    world.run(70, {})
    world.run(12, {5: 100})
    assert len(visitors.list_visits()) == 2


def test_employee_on_several_cameras_never_becomes_a_visitor(world):
    world.identity[100] = "EMP-7"
    world.run(60, {1: 100}, camera_id=1)
    world.run(60, {1: 100}, camera_id=2)
    assert world.alerts() == [] and world.clips == []
    assert sorted(r["camera_id"] for r in visitors.list_employee_visits()) == [1, 2]


def test_history_survives_the_clip_retention(world):
    world.run(12, {1: 100})
    world.run(70, {})
    visitors.purge_expired(now=world.now + 48 * 3600)   # clips and alert rows expire
    assert world.alerts() == []
    v = visitors.list_visits()
    assert len(v) == 1 and v[0]["latest_event_id"] is None   # history kept, clip gone


# ---- background face identification (no overlay viewer needed) -------------

def _face_in(seat_x):
    """A face box in the head part of seat(seat_x)'s body box."""
    return [seat_x + 50, 420, seat_x + 100, 480]


def test_face_identified_inside_a_seated_person_makes_them_an_employee(world, monkeypatch):
    monkeypatch.setattr(visitors, "VERIFY_SECONDS", 0.0)
    world.run(12, {1: 100})
    assert len(world.alerts()) == 1                 # face not identified yet: alerted
    calls = []

    def identify(bbox):
        calls.append(bbox)
        return ("EMP-9", 0.95)

    world.svc.observe_faces(1, (H, W, 3), [_face_in(100)], identify)
    assert calls == [_face_in(100)]
    assert world.alerts() == []                     # wrong alert taken back
    assert [v["employee_id"] for v in visitors.list_employee_visits()] == ["EMP-9"]
    world.run(40 * MIN, {1: 100})
    assert world.alerts() == []
    assert not world.svc.wants_faces(1) or all(v.kind == "employee" for v in world.svc._open[1].values())


def test_faces_outside_the_seated_persons_head_are_not_used(world):
    world.run(5, {1: 100})
    calls = []
    world.svc.observe_faces(1, (H, W, 3), [_face_in(500), [100, 750, 150, 790]], lambda b: calls.append(b) or ("EMP-9", 0.9))
    assert calls == []                              # another seat, and a box at knee height
    assert [v.kind for v in world.svc._open[1].values()] == ["pending"]


def test_unrecognised_face_keeps_them_a_visitor(world):
    world.run(5, {1: 100})
    world.svc.observe_faces(1, (H, W, 3), [_face_in(100)], lambda b: (None, 0.0))
    world.run(10, {1: 100})
    assert len(world.alerts()) == 1


def test_first_alert_waits_for_identification_where_recognition_runs(world, monkeypatch):
    monkeypatch.setattr(visitors, "VERIFY_SECONDS", 20.0)
    monkeypatch.setattr(world.svc, "exclusion_available", lambda cid: True)
    world.run(15, {1: 100})
    assert world.alerts() == []                     # 10 s gap passed, still identifying
    world.run(8, {1: 100})
    a = world.alerts()
    assert len(a) == 1 and 20 <= a[0]["alert_at"] - T0 <= 23


def test_after_a_restart_alerts_wait_for_recognition_to_start(world, monkeypatch):
    monkeypatch.setattr(visitors, "VERIFY_SECONDS", 20.0)
    monkeypatch.setattr(visitors, "RECOGNITION_STARTUP_WAIT_SECONDS", 180.0)
    ready = {"on": False}
    monkeypatch.setattr(world.svc, "exclusion_available", lambda cid: ready["on"])
    monkeypatch.setattr(world.svc, "_recognition_expected", lambda cid: True)
    world.run(60, {1: 100})                         # models still loading
    assert world.alerts() == []
    ready["on"] = True                              # recognition now running
    world.run(15, {1: 100})
    assert world.alerts() == []                     # its own 20 s window starts now
    world.run(8, {1: 100})
    assert len(world.alerts()) == 1


def test_if_recognition_never_starts_the_alert_still_comes(world, monkeypatch):
    monkeypatch.setattr(visitors, "RECOGNITION_STARTUP_WAIT_SECONDS", 180.0)
    monkeypatch.setattr(world.svc, "exclusion_available", lambda cid: False)
    monkeypatch.setattr(world.svc, "_recognition_expected", lambda cid: True)
    world.run(170, {1: 100})
    assert world.alerts() == []
    world.run(15, {1: 100})
    assert len(world.alerts()) == 1


def test_kpis_count_visits_not_alerts_and_never_employees(world, monkeypatch):
    import datetime as _dt
    now = _dt.datetime.now().replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
    world.now = now - 70 * MIN
    world.identity[500] = "EMP-1"
    world.run(65 * MIN, {1: 100, 2: 500})          # a visitor (3 alerts) and an employee, 65 min
    # The visitor's face matched nobody: without it, a sitting while an employee sat there is
    # taken to be that employee's (visitors.mark_employee_sittings).
    from app import face_db
    face_db.init_face_tables()
    with visitors._conn() as conn:
        conn.execute("INSERT INTO face_training_captures (camera_id, track_id, captured_at, image_path, auto_tier, embedding) "
                     "VALUES (1, 1, ?, 'x.jpg', 'unknown', '[1.0, 0.0]')", (now - 60 * MIN,))
    k = visitors.kpis(now=now)
    assert k["visitors_today"] == 1                 # one visit, however many alerts
    assert k["long_stays"] == 1 and k["waiting_now"] == 1
    assert 64 * MIN <= k["average_stay_seconds"] <= 66 * MIN
    assert k["employees_seen_today"] == 1
