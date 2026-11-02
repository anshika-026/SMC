"""
evidence.py

An evidence video clip for every intrusion and loitering alert: about 10 seconds, 5 before the
event and 5 after, playable in a browser (H.264 in an .mp4). Ported from MAINDV's evidence.py
(intrusion-loitering), adapted to SMC:

  - Frames come from SMC's shared clip recorder (clips.py), the same rolling per-camera buffer
    Visitor Management and Footfall use. A camera that is both a guarded intrusion camera and a
    visitor area is buffered once, and nothing is read from a camera twice.
  - When an alert is raised, schedule() asks the recorder for the clip around the event; it waits
    for the post-event seconds and encodes on its background worker, so the camera loop is never
    blocked by encoding.
  - If the camera gave no footage around the event, the alert's clip_status is 'unavailable'; if
    encoding or the playback check fails it is 'failed'. A clip is only ever 'ready' after the
    file has been opened and decoded again. An unrelated or empty video is never served.

Environment: EVIDENCE_CLIPS_ENABLED (default true), EVIDENCE_DIR (default <DATA_DIR>/alert_clips).
The before/after lengths are the recorder's (VISITOR_CLIP_PRE_SECONDS / _POST_SECONDS, 5 and 5).
"""

import logging
import os
import uuid
from pathlib import Path

import cv2

from app import alerts, clips, config, storage

log = logging.getLogger("evidence")

ENABLED = os.environ.get("EVIDENCE_CLIPS_ENABLED", "true").lower() not in ("0", "false", "no")
CLIP_DIR = Path(os.environ.get("EVIDENCE_DIR") or (Path(config.DATA_DIR) / "alert_clips"))
MIN_FRAMES = 2
OWNER = "intrusion"     # this feature's name in the shared recorder


def is_playable(path: Path) -> bool:
    """Open the file again and decode a frame, the way a player would."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    cap = cv2.VideoCapture(str(path))
    try:
        ok, frame = cap.read()
        return bool(ok and frame is not None)
    finally:
        cap.release()


class ClipService:
    def __init__(self, recorder=None, clip_dir: Path | None = None):
        self._recorder = recorder
        self._clip_dir = clip_dir

    @property
    def recorder(self):
        return self._recorder or clips.recorder

    @property
    def clip_dir(self) -> Path:
        return self._clip_dir or CLIP_DIR

    def sync(self, camera_ids: set[int]) -> None:
        """Keep a buffer for exactly these cameras (the ones with an enabled zone)."""
        self.recorder.want(OWNER, set(camera_ids) if ENABLED else set())

    def schedule(self, alert_id: int, camera_id: int, event_ts: float) -> None:
        """Called once for a newly raised alert. Returns immediately; the clip is finished later."""
        if not ENABLED or not self.recorder.is_wanted(camera_id):
            alerts.set_clip(alert_id, None, "unavailable")      # no footage is being buffered for this camera
            return
        path = self.clip_dir / f"alert_{alert_id}_{uuid.uuid4().hex[:10]}.mp4"

        def done(status, frames=None, error=None):
            if status != "ready":
                path.unlink(missing_ok=True)
                # "No video" is the camera's absence, not a recording failure.
                alerts.set_clip(alert_id, None, "unavailable" if error == clips.NO_VIDEO else "failed")
                if error != clips.NO_VIDEO:
                    log.warning("alert %s: evidence clip failed: %s", alert_id, error)
                return
            if (frames or 0) < MIN_FRAMES or not is_playable(path):
                path.unlink(missing_ok=True)
                alerts.set_clip(alert_id, None, "unavailable" if (frames or 0) < MIN_FRAMES else "failed")
                return
            alerts.set_clip(alert_id, storage.to_stored(path), "ready")
            log.info("alert %s: evidence clip saved (%d frames)", alert_id, frames)

        self.recorder.schedule(camera_id, event_ts, path, done)


service = ClipService()
