"""Dwell-time tracking for loitering zones (app/loitering.py), driven by a fake clock."""

from app.loitering import LoiterTracker

DWELL = 15 * 60      # a 15 minute rule
GRACE = 10
CAM = 1


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def make():
    clock = Clock()
    return LoiterTracker(clock=clock, stale_seconds=60), clock


def zone(zone_id, inside, dwell=DWELL, grace=GRACE):
    return (zone_id, dwell, grace, set(inside))


def run(tracker, clock, seconds, zones, step=1.0):
    """Feed the same situation every `step` seconds; returns every alert that fired."""
    fired, end = [], clock.t + seconds
    while clock.t < end:
        clock.advance(step)
        fired += tracker.observe(CAM, zones)
    return fired


def test_person_who_stays_the_whole_duration_raises_exactly_one_alert():
    t, c = make()
    t.observe(CAM, [zone(1, {7})])                                  # enters
    assert run(t, c, DWELL - 5, [zone(1, {7})]) == []               # still short of 15 minutes
    fired = run(t, c, 10, [zone(1, {7})])
    assert [(z, tid) for z, tid, _ in fired] == [(1, 7)]
    assert fired[0][2] >= DWELL
    assert run(t, c, 600, [zone(1, {7})]) == []                     # standing there for ten more minutes: no repeat


def test_leaving_resets_the_timer_and_a_later_visit_starts_from_zero():
    t, c = make()
    t.observe(CAM, [zone(1, {7})])
    run(t, c, DWELL - 60, [zone(1, {7})])                           # 14 minutes inside
    run(t, c, GRACE + 5, [zone(1, set())])                          # leaves for longer than the grace period
    assert t.seconds_inside(CAM, 1, 7) is None
    assert run(t, c, DWELL - 60, [zone(1, {7})]) == []              # back for 14 minutes: the count began again
    assert [tid for _, tid, _ in run(t, c, 120, [zone(1, {7})])] == [7]


def test_a_short_gap_inside_the_grace_period_does_not_reset():
    t, c = make()
    t.observe(CAM, [zone(1, {7})])
    run(t, c, 600, [zone(1, {7})])
    run(t, c, 4, [zone(1, set())])                                  # missed detection / someone walked in front
    fired = run(t, c, 600, [zone(1, {7})])                          # 10 + 10 minutes in total, gap included
    assert [tid for _, tid, _ in fired] == [7]


def test_gap_just_over_the_grace_period_does_reset():
    t, c = make()
    t.observe(CAM, [zone(1, {7})])
    run(t, c, 600, [zone(1, {7})])
    run(t, c, GRACE + 2, [zone(1, set())])
    assert run(t, c, 600, [zone(1, {7})]) == []


def test_alert_rearms_after_the_person_leaves_and_comes_back():
    t, c = make()
    t.observe(CAM, [zone(1, {7})])
    assert len(run(t, c, DWELL + 5, [zone(1, {7})])) == 1           # first stay alerts
    run(t, c, GRACE + 5, [zone(1, set())])                          # leaves
    t.observe(CAM, [zone(1, {7})])
    assert len(run(t, c, DWELL + 5, [zone(1, {7})])) == 1           # second stay alerts once more


def test_people_have_independent_timers():
    t, c = make()
    t.observe(CAM, [zone(1, {1})])
    run(t, c, 600, [zone(1, {1})])
    t.observe(CAM, [zone(1, {1, 2})])                               # a second person arrives 10 minutes later
    fired = run(t, c, 310, [zone(1, {1, 2})])
    assert [tid for _, tid, _ in fired] == [1]                      # only the first has reached 15 minutes
    fired = run(t, c, 600, [zone(1, {1, 2})])
    assert [tid for _, tid, _ in fired] == [2]


def test_zones_have_independent_timers_and_durations():
    t, c = make()
    zones = [zone(1, {7}, dwell=60), zone(2, {7}, dwell=300)]       # same person, two zones, two durations
    t.observe(CAM, zones)
    first = run(t, c, 70, zones)
    assert [(z, tid) for z, tid, _ in first] == [(1, 7)]
    second = run(t, c, 300, zones)
    assert [(z, tid) for z, tid, _ in second] == [(2, 7)]


def test_leaving_one_zone_does_not_reset_the_other():
    t, c = make()
    t.observe(CAM, [zone(1, {7}, dwell=300), zone(2, {7}, dwell=300)])
    run(t, c, 200, [zone(1, {7}, dwell=300), zone(2, set(), dwell=300)])   # zone 2 emptied, zone 1 still occupied
    fired = run(t, c, 110, [zone(1, {7}, dwell=300), zone(2, set(), dwell=300)])
    assert [(z, tid) for z, tid, _ in fired] == [(1, 7)]


def test_cameras_are_isolated():
    t, c = make()
    fired_cam1 = []
    for _ in range(DWELL + 2):                                      # camera 1 keeps reporting person 7 in its zone
        c.advance(1)
        fired_cam1 += t.observe(1, [zone(1, {7})])
    assert [tid for _, tid, _ in fired_cam1] == [7]
    assert t.observe(2, [zone(9, {7})]) == []                       # same track id on another camera is a new person
    assert t.seconds_inside(2, 9, 7) == 0


def test_a_camera_that_went_silent_forgets_its_timers():
    t, c = make()
    t.observe(CAM, [zone(1, {7})])
    run(t, c, DWELL - 30, [zone(1, {7})])
    c.advance(120)                                                  # the camera dropped for two minutes (> stale limit)
    assert t.observe(CAM, [zone(1, {7})]) == []                     # no alert from time nobody saw
    assert t.seconds_inside(CAM, 1, 7) == 0


def test_editing_or_removing_a_zone_drops_its_timers():
    t, c = make()
    t.observe(CAM, [zone(1, {7}), zone(2, {8})])
    run(t, c, 30, [zone(1, {7}), zone(2, {8})])
    t.reset_zone(1)
    assert t.seconds_inside(CAM, 1, 7) is None and t.seconds_inside(CAM, 2, 8) is not None
    t.keep_only(set())
    assert t.seconds_inside(CAM, 2, 8) is None


def test_duration_is_configurable_per_zone():
    t, c = make()
    t.observe(CAM, [zone(1, {7}, dwell=20)])
    assert [tid for _, tid, _ in run(t, c, 25, [zone(1, {7}, dwell=20)])] == [7]
