"""Visitor areas as 3D boxes (visitors.py): the polygon is the floor, an
optional height makes it a box. Areas saved before heights existed load flat
(height 0), and the zone rule still uses the floor polygon (feet inside it).
"""

import json
import sqlite3

import pytest

from app import visitors

FLOOR = [[0.2, 0.5], [0.8, 0.5], [0.9, 0.9], [0.1, 0.9]]


@pytest.fixture
def zones(isolated_db, monkeypatch):
    monkeypatch.setattr(visitors, "DB_PATH", isolated_db)
    monkeypatch.setattr(visitors.service, "refresh", lambda: None)  # no camera streams in tests
    visitors.init_db()
    return isolated_db


def test_3d_area_saves_and_loads_its_height(zones):
    saved = visitors.save_zone(1, FLOOR, True, 0.35)
    assert saved["polygon"] == FLOOR
    assert saved["height"] == 0.35
    assert visitors.get_zone(1)["height"] == 0.35
    assert [z["height"] for z in visitors.list_zones()] == [0.35]


def test_area_saved_without_height_is_flat(zones):
    assert visitors.save_zone(1, FLOOR)["height"] == 0.0


def test_height_must_be_a_fraction_of_the_picture(zones):
    for bad in (-0.1, 1.5, "tall"):
        with pytest.raises(ValueError):
            visitors.save_zone(1, FLOOR, True, bad)


def test_areas_saved_before_3d_areas_load_flat(tmp_path, monkeypatch):
    db_file = tmp_path / "old.db"
    with sqlite3.connect(db_file) as conn:  # the table as it was before the height column
        conn.execute("CREATE TABLE visitor_zones (camera_id INTEGER PRIMARY KEY, polygon TEXT NOT NULL, "
                     "enabled INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL)")
        conn.execute("INSERT INTO visitor_zones VALUES (3, ?, 1, 0)", (json.dumps(FLOOR),))
    monkeypatch.setattr(visitors, "DB_PATH", db_file)
    visitors.init_db()
    visitors.init_db()  # migrating twice is harmless
    zone = visitors.get_zone(3)
    assert zone["polygon"] == FLOOR
    assert zone["height"] == 0.0
    assert zone["enabled"] is True


def test_zone_rule_uses_the_floor_polygon(zones):
    """Feet inside the floor count; a box whose feet are outside the floor does
    not, even when the box reaches up into the drawn 3D volume."""
    svc = visitors.VisitorService()
    svc.identify = lambda cam, bbox, w, h: None
    svc._schedule_clip = lambda *a: None
    w = h = 1000
    on_floor = {"track_id": 1, "bbox": [400, 300, 550, 800], "confidence": 0.9}    # feet at (0.475, 0.8)
    behind = {"track_id": 2, "bbox": [400, 100, 550, 450], "confidence": 0.9}      # feet at (0.475, 0.45), above the floor
    for i in range(visitors.ENTRY_CONFIRM_LOOKS + 1):
        svc.process(1, FLOOR, w, h, [on_floor, behind], 1_800_000_000.0 + i)
    tracks = svc._tracks[1]
    assert tracks[1].inside_looks > 0
    assert 2 not in tracks or tracks[2].inside_looks == 0
