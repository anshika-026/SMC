"""
camera_reader.py

One background thread per RTSP feed that keeps only the most recent frame.
Footfall samples that frame on its own cadence, so a slow Re-ID pass never
backs up the stream. Reconnects forever with exponential backoff; the
camera's state is reported to the dashboard.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import cv2
import numpy as np

from . import config, lifecycle, resilience

log = logging.getLogger("camera")

# FFmpeg options must be set before the first VideoCapture is opened.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", f"rtsp_transport;{config.RTSP_TRANSPORT}|stimeout;10000000")

CONNECTING, LIVE, OFFLINE = "CONNECTING", "LIVE", "OFFLINE"


class CameraReader:
    def __init__(self, camera_id: int, name: str, url: str):
        self.camera_id = camera_id
        self.name = name
        self.url = url
        self.state = CONNECTING
        self.last_error: str | None = None
        self._frame: np.ndarray | None = None
        self._frame_ts = 0.0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"camera-{camera_id}")

    def start(self) -> None:
        self._thread.start()

    def latest(self) -> tuple[np.ndarray | None, float]:
        with self._lock:
            return self._frame, self._frame_ts

    def still_jpeg(self) -> bytes | None:
        frame, _ts = self.latest()
        if frame is None:
            return None
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None

    def _run(self) -> None:
        attempt = 0
        while not lifecycle.stopping():
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if cap.isOpened():
                log.info("camera %s (%s) connected: %s", self.camera_id, self.name, config.redact(self.url))
                attempt = 0
                self.state, self.last_error = LIVE, None
                while not lifecycle.stopping():
                    ok, frame = cap.read()
                    if not ok or frame is None:
                        self.last_error = "stream ended / read failed"
                        break
                    with self._lock:
                        self._frame, self._frame_ts = frame, time.time()
            else:
                self.last_error = "could not open stream (check URL / credentials / network)"
            cap.release()
            if lifecycle.stopping():
                return
            self.state = OFFLINE
            attempt += 1
            delay = resilience.backoff_delay(attempt, config.RTSP_RECONNECT_BASE_SECONDS, config.RTSP_RECONNECT_MAX_SECONDS)
            log.warning("camera %s offline (%s), retrying in %.0fs", self.camera_id, self.last_error, delay)
            if lifecycle.wait(delay):
                return
            self.state = CONNECTING
