"""
ingest.py

RTMP push: cameras publish to the ingest server (MediaMTX, see
docker-compose.yml / mediamtx/mediamtx.yml), which asks this backend whether to
allow every publish and every read (POST /api/ingest/auth -> decide()).

Rules:
  publish  RTMP only, to exactly "<RTMP_APP>/<key>" where <key> belongs to an
           RTMP-push camera whose feed is on. The stream name IS the key, so a
           key can only ever publish its own camera's stream; nothing else can be
           published (no arbitrary stream names, nothing overwritten).
  read     only the backend's own read-only account (INGEST_READ_USER /
           INGEST_READ_PASSWORD), and only a known camera's path. The backend
           reads the stream over internal RTSP with the same reader as every
           other camera, so playback and all AI use it unchanged.
  other    (api, metrics, playback, publishing over RTSP/SRT/WebRTC...) denied,
           except the API for the backend's own account.

Failed publish attempts are rate-limited per source IP, so keys cannot be
guessed by trying. Nothing here logs a key or a full URL.
"""

from __future__ import annotations

import hmac
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from app import camera_db, config, stream_source
from app.ratelimit import SlidingWindowLimiter

log = logging.getLogger("ingest")

# Wrong-key publish attempts allowed per source IP per window before every
# publish from that IP is refused for the rest of the window.
FAILED_PUBLISH_LIMIT = 20
FAILED_PUBLISH_WINDOW = 300.0
_failed_publish = SlidingWindowLimiter(FAILED_PUBLISH_LIMIT, FAILED_PUBLISH_WINDOW)


@dataclass
class Decision:
    allowed: bool
    camera_id: int | None = None
    reason: str = ""


def _same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a.encode(), b.encode())


def _is_backend_reader(req: dict) -> bool:
    return (_same(str(req.get("user") or ""), config.INGEST_READ_USER)
            and _same(str(req.get("password") or ""), config.INGEST_READ_PASSWORD))


def _key_from_path(path: str) -> str | None:
    prefix = f"{config.RTMP_APP}/"
    if not path.startswith(prefix):
        return None
    key = path[len(prefix):]
    return key if stream_source.is_stream_key(key) else None


def decide(req: dict, lookup=camera_db.get_camera_by_stream_key) -> Decision:
    """req: the JSON body the ingest server posts (user, password, ip, action,
    path, protocol, id, query)."""
    action = str(req.get("action") or "")
    protocol = str(req.get("protocol") or "")
    path = str(req.get("path") or "").strip("/")
    ip = str(req.get("ip") or "")

    if action == "publish":
        if _failed_publish.retry_after(ip) > 0:
            return Decision(False, reason="too many failed publish attempts from this address")
        if protocol != "rtmp":
            return Decision(False, reason="publishing is only accepted over RTMP")
        key = _key_from_path(path)
        cam = lookup(key) if key else None
        if cam is None:
            _failed_publish.hit(ip)
            return Decision(False, reason="unknown stream key")
        if not cam.get("live_feed_enabled", 1):
            return Decision(False, camera_id=cam["id"], reason="camera feed is switched off")
        return Decision(True, camera_id=cam["id"], reason="publish")

    if action == "read":
        if not _is_backend_reader(req):
            return Decision(False, reason="reading is only allowed for the backend")
        key = _key_from_path(path)
        cam = lookup(key) if key else None
        if cam is None:
            return Decision(False, reason="unknown stream")
        return Decision(True, camera_id=cam["id"], reason="read")

    if action == "api" and _is_backend_reader(req):
        return Decision(True, reason="api")
    return Decision(False, reason=f"action not allowed: {action or 'none'}")


def hook_secret_ok(given: str | None) -> bool:
    return _same(given or "", config.INGEST_HOOK_SECRET)


# --- keeping a pushed camera read -------------------------------------------------------
# A pulled (RTSP) camera is read while something subscribes to it (analytics
# keep-alives, viewers). A pushed camera must be read as soon as it starts
# publishing - otherwise its video reaches the ingest server but never the
# backend: no live view, no AI, and it never shows Online. The first accepted
# publish therefore subscribes a background keep-alive; it stays (the reader
# simply retries every few seconds while the camera is not publishing), so the
# camera goes Online/Offline with its real stream.
import threading

_reading: set[int] = set()
_reading_lock = threading.Lock()


class _KeepAlive:
    """A collector subscription: keeps the read loop alive, takes no frames."""

    def put_nowait(self, item):
        pass


def ensure_reading(camera_id: int) -> bool:
    """Start reading this camera if nothing does yet. True if it was started now."""
    with _reading_lock:
        if camera_id in _reading:
            return False
        _reading.add(camera_id)
    from app import camera_stream

    camera_stream.get_stream(camera_id).subscribe(_KeepAlive(), is_collector=True, wants_frames=False)
    return True


def forget(camera_id: int) -> None:
    """The camera was deleted: a new camera that reuses the id starts clean."""
    with _reading_lock:
        _reading.discard(camera_id)


# --- setup details -----------------------------------------------------------------------

class NotConfigured(RuntimeError):
    pass


def setup_for(key: str) -> dict:
    """Server URL, key and one-field URL for a camera, from the configured
    public ingest address. NotConfigured when no public host is set (never a
    made-up address)."""
    if not config.RTMP_PUBLIC_HOST:
        raise NotConfigured("RTMP push is not configured on this server (RTMP_PUBLIC_HOST is not set).")
    scheme = config.RTMP_PUBLIC_SCHEME if config.RTMP_PUBLIC_SCHEME in ("rtmp", "rtmps") else "rtmp"
    return stream_source.push_setup(key, config.RTMP_PUBLIC_HOST, config.RTMP_PUBLIC_PORT, scheme,
                                    config.RTMP_APP, config.RTMP_FULL_URL_FORMAT)


# --- ingest server API (status, kicking an old publisher) ---------------------------------

def _api(method: str, path: str, timeout: float = 2.0) -> dict | None:
    import base64

    req = urllib.request.Request(f"{config.INGEST_API_URL}{path}", method=method)
    if config.INGEST_READ_PASSWORD:
        token = base64.b64encode(f"{config.INGEST_READ_USER}:{config.INGEST_READ_PASSWORD}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:   # noqa: S310 - internal, configured URL
            body = resp.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def ingest_ready(key: str) -> bool | None:
    """True/False: whether the ingest server is currently receiving this camera's
    stream. None: the ingest server could not be asked."""
    try:
        data = _api("GET", f"/v3/paths/get/{stream_source.ingest_path(key, config.RTMP_APP)}")
    except Exception:
        return None
    return bool(data and data.get("ready"))


def kick_publishers(key: str) -> int:
    """Disconnect whoever is publishing with this (old) key - used after a key is
    rotated, since the ingest server only checks a key when a session starts."""
    path = stream_source.ingest_path(key, config.RTMP_APP)
    kicked = 0
    try:
        conns = (_api("GET", "/v3/rtmpconns/list") or {}).get("items", [])
        for c in conns:
            if c.get("path") == path and c.get("id"):
                _api("POST", f"/v3/rtmpconns/kick/{c['id']}")
                kicked += 1
    except Exception:
        log.warning("could not disconnect the old publisher after a key rotation (ingest server unreachable)")
    return kicked


def stream_status(camera_id: int, key: str, stream=None, now: float | None = None) -> dict:
    """Online only on real video: frames reached this backend in the last 15 s.
    'receiving' = the ingest server has the stream but no frame decoded yet."""
    now = time.time() if now is None else now
    last = getattr(stream, "last_frame_at", None) if stream is not None else None
    if last and now - last <= 15:
        return {"status": "online", "last_frame_at": last}
    ready = ingest_ready(key)
    return {"status": "receiving" if ready else ("offline" if ready is False else "unknown"), "last_frame_at": last}
