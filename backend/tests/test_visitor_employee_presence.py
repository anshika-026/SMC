"""Employees seen in a visitor area (visitors.list_employee_visits and the
/api/visitors/employee-* routes): listed apart from visitors, with their
original stored face captures, and read-only on the data."""

import sqlite3
import time

import pytest

from app import employee_directory, face_db, visitors
from tests.conftest import bearer

CAM = 4


@pytest.fixture
def world(isolated_db, tmp_path, monkeypatch):
    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    visitors.init_db()
    monkeypatch.setattr(employee_directory, "get_employee",
                        lambda eid: {"8": {"name": "Asha Rao", "company": "Operations"}}.get(str(eid)))
    (tmp_path / "face_training").mkdir(exist_ok=True)
    return isolated_db


def stay(db, emp, entered, last_seen, left=None, cam=CAM):
    with sqlite3.connect(db) as c:
        return c.execute("INSERT INTO visitor_area_employee_log (camera_id, camera_name, employee_id, entered_at, last_seen, left_at)"
                         " VALUES (?, 'Cam3', ?, ?, ?, ?)", (cam, emp, entered, last_seen, left)).lastrowid


def capture(db, tmp_path, emp, at, cam=CAM, **cols):
    rel = f"face_training/c_{emp}_{int(at * 1000)}.jpg"
    (tmp_path / rel).write_bytes(b"\xff\xd8 original jpeg")
    cid = face_db.add_training_capture(cam, 1, rel, None, auto_employee_id=emp)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE face_training_captures SET captured_at = ? WHERE id = ?", (at, cid))
        for k, v in cols.items():
            c.execute(f"UPDATE face_training_captures SET {k} = ? WHERE id = ?", (v, cid))
    return cid


def counts(db):
    with sqlite3.connect(db) as c:
        return (c.execute("SELECT COUNT(*) FROM visitor_area_employee_log").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM visitor_events").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM face_training_captures").fetchone()[0])


def test_present_now_and_stale_open_stays(world):
    now = time.time()
    stay(world, "8", now - 300, now - 2)                 # being written right now
    stay(world, "8", now - 7200, now - 3600)             # left_at never written (backend restarted)
    stay(world, "8", now - 9000, now - 8000, now - 8000)
    rows = visitors.list_employee_visits()
    assert [r["status"] for r in rows] == ["inside", "left", "left"]
    assert rows[1]["left_at"] == pytest.approx(now - 3600)   # shown as left at its last sighting


def test_one_sitting_split_over_track_ids_is_one_presence(world):
    """The real 2 Nov rows: one person seated 16:38:32-16:48:48 got five log
    rows (new track ids, overlapping). They show as one presence; a separate
    sitting later, and another employee at the same time, stay separate."""
    base = time.time() - 6 * 3600
    t = lambda m, s=0: base + m * 60 + s
    stay(world, "8", t(0, 0), t(1, 57), t(1, 57))        # 16:38:32-16:40:29
    stay(world, "8", t(2, 2), t(4, 33))                  # 16:40:34-16:43:05, left_at never written
    stay(world, "8", t(4, 14), t(4, 23))                 # 16:42:46-16:42:55, overlaps
    stay(world, "8", t(5, 2), t(10, 16), t(10, 16))      # 16:43:34-16:48:48
    stay(world, "8", t(8, 41), t(9, 2), t(9, 2))         # 16:47:13-16:47:34, inside the previous one
    stay(world, "8", t(40), t(45), t(45))                # a later, separate sitting
    stay(world, "4", t(3), t(6), t(6))                   # someone else meanwhile
    before = counts(world)
    rows = visitors.list_employee_visits()
    ravi = [r for r in rows if r["employee_id"] == "8"]
    assert len(ravi) == 2
    first = ravi[1]
    assert first["arrived_at"] == pytest.approx(t(0)) and first["left_at"] == pytest.approx(t(10, 16))
    assert len(first["log_ids"]) == 5 and first["status"] == "left"
    assert len([r for r in rows if r["employee_id"] == "4"]) == 1
    assert counts(world) == before   # display only


def test_api_lists_employees_with_their_original_snapshots(api, admin_token, world, tmp_path):
    now = time.time()
    sid = stay(world, "8", now - 600, now - 1)
    inside = [capture(world, tmp_path, "8", now - 500), capture(world, tmp_path, "8", now - 100)]
    capture(world, tmp_path, "8", now - 5000)                          # another time: not this stay
    capture(world, tmp_path, "9", now - 300)                           # someone else
    capture(world, tmp_path, "8", now - 300, cam=1)                    # another camera
    capture(world, tmp_path, "8", now - 200, label_status="rejected")  # rejected
    before = counts(world)

    h = bearer(admin_token)
    r = api.get("/api/visitors/employee-visits", headers=h).json()["visits"][0]
    assert r["name"] == "Asha Rao" and r["department"] == "Operations" and r["person_type"] == "employee"
    assert r["status"] == "inside" and r["snapshot_count"] == 2 and r["snapshot_id"] in inside
    shots = api.get(f"/api/visitors/employee-visits/{sid}/snapshots", headers=h).json()["snapshots"]
    assert [s["capture_id"] for s in shots] == inside
    img = api.get(f"/api/visitors/employee-snapshots/{inside[0]}", headers=h)
    assert img.status_code == 200 and img.content == b"\xff\xd8 original jpeg"
    assert counts(world) == before          # nothing created, copied or changed


def test_snapshot_route_serves_only_visitor_area_employee_captures(api, admin_token, world, tmp_path):
    now = time.time()
    stay(world, "8", now - 600, now - 1)
    elsewhere = capture(world, tmp_path, "8", now - 300, cam=1)
    unrelated = capture(world, tmp_path, "8", now - 50_000)
    h = bearer(admin_token)
    assert api.get(f"/api/visitors/employee-snapshots/{elsewhere}", headers=h).status_code == 404
    assert api.get(f"/api/visitors/employee-snapshots/{unrelated}", headers=h).status_code == 404
    assert api.get("/api/visitors/employee-snapshots/999999", headers=h).status_code == 404
    assert api.get(f"/api/visitors/employee-snapshots/{elsewhere}").status_code == 401


def test_employees_stay_out_of_visitor_counts(api, admin_token, world):
    now = time.time()
    stay(world, "8", now - 600, now - 1)
    k = api.get("/api/visitors/kpis", headers=bearer(admin_token)).json()
    assert k["visitors_today"] == 0 and k["employees_seen_today"] == 1
