"""Unique footfall across the two SMC cameras: both share one identity
gallery, so a person counts once however many cameras see them.

Drives the real FootfallService / reid_worker / gallery / fusion / reid_db
code against a throwaway SQLite file. Only the person detector and the
Re-ID embedder are faked (no camera or model download needed): two views of
one person at cosine 0.94, different people ~0.

Run from backend/:  python -m pytest tests -v
"""

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, footfall, footfall_routes
from app.reid import config as reid_config
from app.reid import reid_db

DIM = 512
SAME_PERSON_SIMILARITY = 0.94
CAM_1, CAM_2 = 1, 2
CAMERAS = [
    {"id": CAM_1, "name": "Camera 1", "url": "rtsp://user:pw@10.0.0.1:8554/video/live"},
    {"id": CAM_2, "name": "Camera 2", "url": "rtsp://user:pw@10.0.0.1:8555/video/live"},
]


def person_embedding(seed: int) -> np.ndarray:
    v = np.random.RandomState(seed).randn(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def another_view(base: np.ndarray, seed: int) -> np.ndarray:
    noise = np.random.RandomState(seed).randn(DIM).astype(np.float32)
    noise -= float(noise @ base) * base
    noise /= np.linalg.norm(noise)
    v = SAME_PERSON_SIMILARITY * base + np.sqrt(1 - SAME_PERSON_SIMILARITY**2) * noise
    return v / np.linalg.norm(v)


class FakeScene:
    """What each camera currently sees: track_id -> person seed."""

    def __init__(self):
        self.visible: dict[int, int] = {}
        self._view_seed = 10_000

    @staticmethod
    def bbox_for(track_id: int) -> list[float]:
        x = 20 + 150 * (track_id % 3)
        return [x, 50, x + 110, 330]  # passes the size/aspect quality gate

    def next_view_seed(self) -> int:
        self._view_seed += 1
        return self._view_seed


class FakeTracker:
    def __init__(self, scene: FakeScene):
        self.scene = scene

    def update(self, frame):
        return [{"track_id": tid, "bbox": FakeScene.bbox_for(tid), "confidence": 0.9} for tid in self.scene.visible]


class FakeEmbedder:
    def __init__(self, scene: FakeScene):
        self.scene = scene

    def embed(self, frame, bbox):
        for tid, person in self.scene.visible.items():
            if FakeScene.bbox_for(tid) == bbox:
                return another_view(person_embedding(person), self.scene.next_view_seed())
        return None


# Textured mid-grey noise: sharp and well-lit enough to pass reid_quality.
FRAME = np.random.RandomState(0).randint(60, 200, (400, 520, 3)).astype(np.uint8)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(reid_db, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(reid_db, "SNAPSHOTS_DIR", tmp_path / "snaps")
    monkeypatch.setattr(reid_config, "REID_MOT_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(config, "cameras", lambda: CAMERAS)
    reid_db.init_db()
    service = footfall.FootfallService()
    service._gallery = footfall._SharedGallery()
    service.scenes = {}
    for cid in (CAM_1, CAM_2):
        runner = footfall._GateRunner(cid, service._gallery)
        scene = FakeScene()
        runner.state.tracker = FakeTracker(scene)
        runner.state.embedder = FakeEmbedder(scene)
        service._runners[cid] = runner
        service.scenes[cid] = scene
    return service


def walk_past(service, cam: int, person: int, track_id: int, frames: int = 6):
    scene = service.scenes[cam]
    scene.visible = {track_id: person}
    for _ in range(frames):
        service._process(service._runners[cam], FRAME)
    scene.visible = {}


def per_camera(service) -> dict:
    return {g["name"]: g["unique_today"] for g in service.summary()["gates"]}


def test_same_person_on_both_cameras_is_one(svc):
    walk_past(svc, CAM_1, person=1, track_id=1)
    walk_past(svc, CAM_2, person=1, track_id=1)
    s = svc.summary()
    assert s["unique_today"] == 1
    assert per_camera(svc) == {"Camera 1": 1, "Camera 2": 1}
    assert set(s["visitors"][0]["gates"]) == {"Camera 1", "Camera 2"}


def test_returning_person_is_still_one(svc):
    walk_past(svc, CAM_1, person=1, track_id=1)
    walk_past(svc, CAM_2, person=1, track_id=5)
    walk_past(svc, CAM_1, person=1, track_id=9)
    assert svc.summary()["unique_today"] == 1
    assert reid_db.count_unique_lifetime() == 1


def test_different_people_are_counted_separately(svc):
    walk_past(svc, CAM_1, person=1, track_id=1)
    walk_past(svc, CAM_2, person=2, track_id=1)
    walk_past(svc, CAM_2, person=1, track_id=2)
    assert svc.summary()["unique_today"] == 2


def test_reset_restarts_count(svc):
    walk_past(svc, CAM_1, person=1, track_id=1)
    assert svc.reset()["people_removed"] == 1
    assert svc.summary()["unique_today"] == 0


def test_summary_lists_only_configured_cameras(svc):
    assert [g["camera_id"] for g in svc.summary()["gates"]] == [CAM_1, CAM_2]


def test_credentials_are_encoded_and_redacted():
    url = config.with_credentials("rtsp://144.79.198.149:8554/video/live?channel=1&subtype=0", "admin", "p@ss:1")
    assert url == "rtsp://admin:p%40ss%3A1@144.79.198.149:8554/video/live?channel=1&subtype=0"
    assert config.redact(url) == "rtsp://admin:***@144.79.198.149:8554/video/live?channel=1&subtype=0"


def test_api_token_and_unknown_camera(svc, monkeypatch):
    monkeypatch.setattr(footfall_routes, "service", svc)
    app = FastAPI()
    app.include_router(footfall_routes.router)
    client = TestClient(app)
    assert client.get("/api/footfall/summary").status_code == 200
    assert client.put("/api/footfall/cameras/99/zone", json={"roi": None}).status_code == 404

    monkeypatch.setattr(config, "API_TOKEN", "secret")
    assert client.get("/api/footfall/summary").status_code == 401
    assert client.get("/api/footfall/summary", headers={"Authorization": "Bearer secret"}).status_code == 200
