"""Visitor area: an employee detected twice is not a visitor; interrupted clips say so."""

from app import visitors


def test_a_second_box_over_a_recognised_employee_is_that_employee():
    svc = visitors.VisitorService()
    emp = visitors._Visit("e", 1, [100, 100, 300, 500], 1000.0)
    emp.kind, emp.employee_id = "employee", "PUSH"
    twin = visitors._Visit("t", 2, [110, 105, 305, 495], 1000.0)          # the same seat, same person
    beside = visitors._Visit("b", 3, [400, 100, 600, 500], 1000.0)        # someone in the next seat
    svc._open[4] = {"e": emp, "t": twin, "b": beside}
    assert svc.employee_twin(4, twin, 1001.0) == "PUSH"
    assert svc.employee_twin(4, beside, 1001.0) is None
    emp.last_in = 900.0                                                    # the employee left long ago
    assert svc.employee_twin(4, twin, 1001.0) is None


def test_an_interrupted_clip_is_marked_failed(isolated_db, monkeypatch):
    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    visitors.init_db()
    with visitors._conn() as conn:
        for i, age in ((1, 3600), (2, 60)):
            conn.execute("INSERT INTO visitor_events (id, camera_id, camera_name, track_id, entered_at, last_in_zone_at, created_at, status, clip_status, expires_at)"
                         " VALUES (?, 4, 'Cam3', 1, ?, ?, ?, 'left', 'pending', ?)", (i, 10_000.0 - age, 10_000.0 - age, 10_000.0 - age, 99_999.0))
    assert visitors.fail_stuck_clips(now=10_000.0) == 1
    with visitors._conn() as conn:
        assert [r[0] for r in conn.execute("SELECT clip_status FROM visitor_events ORDER BY id")] == ["failed", "pending"]
