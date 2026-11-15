import asyncio
import contextlib
import logging
import os
import sqlite3
import sys
import threading
import time
from typing import Literal

# Must be set before numpy/torch/onnxruntime get imported (transitively, by
# face_pipeline below) to take effect. Each of the 3 concurrent camera
# threads runs its own YOLO/InsightFace inference calls against the same
# shared model instances (see face_pipeline.CameraFacePipeline's
# class-level _yolo/_yolo_person/_arcface); left at their library default,
# each call tries to use every CPU core, so 3 cameras running "at once"
# means 3x oversubscription fighting itself for the same 8 cores rather
# than 3 cameras actually running in parallel. Capping per-call threads
# lets the OS scheduler give each camera's thread a fair, non-thrashing
# share instead. Measured: this machine's backend process was pinned at
# ~540% CPU (of 8 cores) with live video down to ~0.4 fps against an 8 fps
# target before this change.
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")

from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import cv2

from . import config, logging_setup, models

# Every camera runs several CPU-heavy Python threads (face/person detection,
# recognition, tracking) in this process, and all of them share Python's global
# interpreter lock with the thread that serves the API and websockets. With the
# default 5 ms switch interval and dozens of busy threads, the API thread waited
# so long for its turn that requests took 25 s+ or timed out (measured with four
# cameras on an 8-core laptop) while the backend used under 3 cores. Handing the
# lock over more often keeps the API responsive at a small cost to AI throughput.
sys.setswitchinterval(float(os.environ.get("PY_GIL_SWITCH_INTERVAL", "0.0005")))

# Diagnostics: `docker kill -s USR1 smc-backend` (or kill -USR1 <pid>) writes every
# thread's current Python stack to stderr - shows what a stalled API is waiting on.
if hasattr(__import__("signal"), "SIGUSR1"):
    import faulthandler
    import signal

    faulthandler.register(signal.SIGUSR1, all_threads=True)

logging_setup.configure()
# Before any ML library is imported below: no runtime model downloads in
# offline mode (see models.py).
models.apply_offline_environment()

from . import audit, auth, camera_db, camera_stream, employee_directory, face_collection, face_db, face_pipeline, face_training_scheduler, license_db, ratelimit  # noqa: E402
from .staff import routes as staff_routes  # noqa: E402
from .staff.service import service as staff_service  # noqa: E402
from . import alerts, alerts_routes, analytics_routes, analytics_settings, attendance, attendance_routes, desk_db, desk_routes, desks, face_routes, insights_routes, face_training_routes, capture_review_routes, footfall, footfall_routes, ingest, license_routes, rtsp_reader, stream_source, intrusion, intrusion_routes, visitor_registry, visitor_registry_routes, visitor_routes, visitors, zone_routes  # noqa: E402
from . import health_routes, heap, lifecycle, person_detection, retention  # noqa: E402
from . import master_link, sso_routes  # noqa: E402
from . import whatsapp, whatsapp_routes  # noqa: E402
from .ratelimit import SlidingWindowLimiter  # noqa: E402

log = logging.getLogger("main")

# Same oversubscription fix as the OMP/BLAS env vars above, for OpenCV's
# own internal parallelism (JPEG decode/encode, resize) — otherwise each of
# the 3 camera threads' cv2 calls also each try to claim every core.
cv2.setNumThreads(2)
try:
    import torch

    torch.set_num_threads(2)
except ImportError:
    pass

@contextlib.asynccontextmanager
async def lifespan(_app):
    # Startup is synchronous and ordered: refuse an unsafe production
    # configuration, create/migrate tables, then start background services.
    check_configuration()
    init_databases()
    start_services()
    # Keep garbage collection from freezing the server (see heap.py).
    heap.freeze("startup")
    heap.install()
    log.info("startup complete (APP_ENV=%s)", config.APP_ENV)
    try:
        yield
    finally:
        shutdown()


app = FastAPI(
    lifespan=lifespan,
    title="Deco Vision API",
    docs_url="/docs" if config.ENABLE_API_DOCS else None,
    redoc_url=None,
    openapi_url="/openapi.json" if config.ENABLE_API_DOCS else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# Largest body any endpoint legitimately takes: one image upload plus
# multipart overhead. Checked against Content-Length before the body is
# read, so an oversized upload is refused without being buffered.
_MAX_BODY_BYTES = config.MAX_UPLOAD_BYTES + 256 * 1024


@app.middleware("http")
async def _limits_and_headers(request: Request, call_next):
    length = request.headers.get("content-length")
    if length is not None:
        try:
            too_big = int(length) > _MAX_BODY_BYTES
        except ValueError:
            return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
        if too_big:
            return JSONResponse({"detail": "Request body too large"}, status_code=413)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    # Full traceback to the log, nothing internal to the caller.
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)

def _licensed(feature: str) -> list:
    """Router dependency: the Master Portal license must include `feature`
    (always passes when INTEGRATION_MODE=standalone; see master_link.py)."""
    return [Depends(master_link.require(feature))]


app.include_router(face_routes.router, dependencies=_licensed("face_recognition"))
app.include_router(face_training_routes.router, dependencies=_licensed("face_training"))
app.include_router(capture_review_routes.router, dependencies=_licensed("face_recognition"))
app.include_router(license_routes.router)
app.include_router(footfall_routes.router, dependencies=_licensed("footfall_analytics"))
app.include_router(attendance_routes.router, dependencies=_licensed("attendance"))
app.include_router(desk_routes.router)       # desk analytics gated per route in desk_routes (camera stills aren't)
app.include_router(insights_routes.router)   # dashboard: always; workforce overview gated in insights_routes
app.include_router(alerts_routes.router)
app.include_router(analytics_routes.router)
app.include_router(visitor_routes.router, dependencies=_licensed("visitor_management"))
app.include_router(visitor_registry_routes.router, dependencies=_licensed("visitor_management"))
app.include_router(zone_routes.router)       # Zones & areas list (intrusion zones gated in zones.py by the license)
app.include_router(intrusion_routes.router, dependencies=_licensed("intrusion_detection"))
app.include_router(staff_routes.router, dependencies=_licensed("attendance"))
app.include_router(health_routes.router)
app.include_router(sso_routes.router)
app.include_router(whatsapp_routes.router)    # SMC Connect (WhatsApp Business), admin only
app.include_router(whatsapp_routes.webhook)   # Meta delivery-status webhook (public, signature-checked)
staff_routes.register_websockets(app)


def _enable_wal() -> None:
    """Attendance, desk analytics, footfall and face-training capture all
    write to data/app.db through the day, and in SQLite's default rollback
    mode a reader blocks writers (and vice versa) — which surfaced as
    "database is locked" on login. WAL lets reads proceed during a write.
    It's a persistent property of the database file, so this is a no-op
    after the first run."""
    from app import db

    conn = db.open_connection(camera_db.DB_PATH)
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        logging.getLogger("main").info("database journal mode: %s", mode)
    finally:
        conn.close()


def check_configuration() -> None:
    """Fatal in production, warnings elsewhere — see config.validate()."""
    problems = config.validate()
    for p in problems:
        (log.error if config.IS_PRODUCTION else log.warning)("configuration: %s", p)
    if problems and config.IS_PRODUCTION:
        raise RuntimeError("Refusing to start with an unsafe production configuration: " + "; ".join(problems))
    if config.JWT_SECRET_IS_EPHEMERAL:
        log.warning("configuration: JWT_SECRET not set; license QR codes will stop verifying after a restart")


def init_databases() -> None:
    _enable_wal()
    camera_db.init_db()
    analytics_settings.init_db()
    face_db.init_face_tables()
    license_db.init_db()
    master_link.init_db()
    auth.init_db()
    audit.init_db()
    auth.bootstrap_admin_from_env()
    if auth.count_admin_users() == 0:
        msg = "no admin account exists; create one with: python -m app.manage create-admin --email you@example.com"
        if config.IS_PRODUCTION:
            raise RuntimeError(msg)
        log.warning(msg)
    attendance.init_db()
    from app import presence, workforce

    workforce.init_db()   # policy, holidays, leave types/allocations/requests, corrections (additive tables)
    presence.init_db()    # Main Hall camera choice for presence investigation
    alerts.init_db()
    visitors.init_db()
    visitor_registry.init_db()   # visitor profiles, visits, visit events, visit media (additive tables)
    intrusion.init_db()          # intrusion / loitering zones and detections (MAINDV, additive columns)
    whatsapp.init_db()           # SMC Connect: recipients, rules, message log (additive tables)
    desk_db.init_db()


def start_services() -> None:
    # Always start the expiry watcher (cheap, idempotent) so a session
    # started later still gets watched, then resume whatever collection
    # session was running before a restart (if its planned end time hasn't
    # already passed) — see face_collection.py for why this can never
    # create a duplicate worker per camera.
    # Master Portal license refresher (no-op unless INTEGRATION_MODE=master).
    master_link.ensure_started()
    face_collection.ensure_expiry_watcher_started()
    face_collection.resume_if_needed()
    # Periodic background retraining trigger (new labeled samples
    # accumulate -> auto-retrain) — separate daemon thread, never blocks
    # the camera pipelines or request handling. See
    # face_training_scheduler.py.
    face_training_scheduler.ensure_scheduler_started()
    # Unique footfall across entry gates — keeps every "Entry/Exit" camera
    # streaming and counting, viewer or not. See footfall.py.
    footfall.service.start()
    # Desk analytics + keeping face-recognition cameras streaming all day.
    desks.service.start()
    # Camera-offline / footfall-stopped alert monitor.
    alerts.start()
    # Visitor Management: entry events, 10 s clips and the 24 h clean-up (see visitors.py).
    visitors.service.start()
    # Visitor records: expired snapshot/clip clean-up (see visitor_registry.py).
    visitor_registry.start()
    # Intrusion / loitering zones: alerts with evidence clips (see intrusion.py, evidence.py).
    intrusion.service.start()
    # SMC Connect: WhatsApp notifications for new alerts, visitor check-ins and daily reports.
    whatsapp.service.start()
    # Staff Count at entrance cameras (see app/staff/).
    staff_service.start()
    # Hourly cleanup of expired sessions, old captures/snapshots, backups (retention.py).
    retention.start()


def shutdown() -> None:
    """SIGTERM/SIGINT (uvicorn runs the lifespan exit): stop taking new
    work, stop every camera read loop and its RTSP reader child process,
    stop inference executors and background loops. Nothing is left running
    and no orphan reader processes survive the backend."""
    log.info("shutdown: stopping background services")
    lifecycle.begin_shutdown()
    try:
        camera_stream.stop_all(timeout=10)
    except Exception:
        log.exception("shutdown: stopping camera streams failed")
    try:
        person_detection.service.shutdown()
    except Exception:
        log.exception("shutdown: stopping person detection failed")
    log.info("shutdown complete")


# ---------------------------------------------------------------------------
# Cameras
# ---------------------------------------------------------------------------

class CameraIn(BaseModel):
    name: str
    site: str
    cam_code: str = ""
    purpose: str = "GENERAL"
    host: str = ""
    port: int = 554
    user: str = ""
    password: str | None = None
    stream_path: str = "/h264/ch1/sub/av_stream"
    vendor: str = ""
    live_feed_enabled: bool = True
    attendance_tracking: bool = True
    # How the camera is read. Existing cameras (and any client that does not send
    # this) are RTSP and use host/port/user/password/stream_path as before. An RTMP
    # camera sends its full link (rtmp://host/app/stream-key) in source_url.
    # rtmp_push: the camera publishes to us; SMC generates its key (see ingest.py).
    protocol: Literal["rtsp", "rtmp", "rtmp_push"] = "rtsp"
    source_url: str | None = None


class CameraUpdate(BaseModel):
    name: str | None = None
    site: str | None = None
    cam_code: str | None = None
    purpose: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    stream_path: str | None = None
    vendor: str | None = None
    live_feed_enabled: bool | None = None
    attendance_tracking: bool | None = None
    protocol: Literal["rtsp", "rtmp", "rtmp_push"] | None = None
    source_url: str | None = None


# Fields that change how (or where) a camera is read: editing any of them
# reconnects a running camera so the change takes effect without a restart.
_CONNECTION_FIELDS = {"protocol", "source_url", "host", "port", "user", "password", "stream_path"}


def _push_fields() -> dict:
    """An RTMP-push camera has no address of its own: it is read from the ingest
    server by its key. host is a fixed, non-secret marker (see stream_source)."""
    return {"protocol": stream_source.PUSH_PROTOCOL, "host": stream_source.PUSH_HOST,
            "port": config.RTMP_PUBLIC_PORT, "user": "", "password": "", "stream_path": "", "source_url": ""}


def _assign_new_key(camera_id: int) -> str:
    """A fresh unique key for a camera (a collision, ~2^-96, just retries)."""
    for _ in range(5):
        key = stream_source.generate_stream_key()
        try:
            camera_db.set_stream_key(camera_id, key)
            return key
        except sqlite3.IntegrityError:
            continue
    raise HTTPException(status_code=500, detail="Could not create a stream key, try again")


def _setup_payload(camera_id: int, key: str) -> dict:
    """Setup details for the RTMP setup screen, or a clear 'not configured'."""
    try:
        return {"camera_id": camera_id, **ingest.setup_for(key)}
    except ingest.NotConfigured as e:
        raise HTTPException(status_code=503, detail=str(e))


def _rtmp_fields(source_url: str | None) -> dict:
    """Validated RTMP link plus the non-secret host/port derived from it (shown in
    the camera list). The link itself only ever goes into source_url."""
    try:
        url = stream_source.validate_rtmp_url(source_url)
    except stream_source.SourceError as e:
        raise HTTPException(status_code=422, detail=str(e))
    host, port = stream_source.rtmp_host_port(url)
    return {"source_url": url, "host": host, "port": port, "user": "", "password": "", "stream_path": ""}


@app.get("/api/cameras")
def list_cameras(principal: dict = Depends(auth.get_principal)):
    """Tenant isolation lives here, not in the React UI: an admin
    principal sees every camera (unchanged), but a client principal only
    ever sees the cameras their own license has been assigned — enforced
    fresh against the DB on every call, not from anything the browser
    claims about itself. Previously this endpoint had no authentication
    at all and returned every camera to anyone who called it."""
    cameras = camera_db.list_cameras()
    if principal["principal_type"] == auth.CLIENT:
        lic = auth.load_active_client_license(principal)
        allowed_ids = {c["id"] for c in license_db.list_cameras_for_license(lic["id"])}
        cameras = [c for c in cameras if c["id"] in allowed_ids]
    return cameras


_camera_limit_lock = threading.Lock()


def _report_cameras_soon() -> None:
    """Tell the Master Portal about a camera change without holding up the request."""
    if master_link.enabled():
        threading.Thread(target=master_link.report_cameras, name="master-camera-report", daemon=True).start()


@app.post("/api/cameras")
def create_camera(payload: CameraIn, principal: dict = Depends(auth.require_admin)):
    fields = payload.model_dump(exclude={"name", "site"})
    if payload.protocol == "rtmp":
        fields.update(_rtmp_fields(payload.source_url))
    elif payload.protocol == stream_source.PUSH_PROTOCOL:
        fields.update(_push_fields())
    else:
        fields["source_url"] = ""      # an RTSP camera never carries an RTMP link
    # The Master Portal license's camera limit (none in standalone mode).
    # Checked and added under one lock so two adds at once can't both slip in.
    with _camera_limit_lock:
        limit = master_link.max_cameras()
        if limit is not None and len(camera_db.list_cameras()) >= limit:
            raise HTTPException(
                status_code=403,
                detail=f"Your SMC license allows {limit} camera(s) and all are in use. "
                       "Contact your administrator to increase the limit.",
            )
        camera_id = camera_db.add_camera(payload.name, payload.site, **fields)
    _report_cameras_soon()
    audit.record("camera.create", principal, target=f"camera:{camera_id}", name=payload.name, protocol=payload.protocol)
    cam = camera_db.get_camera(camera_id)
    if payload.protocol == stream_source.PUSH_PROTOCOL:
        key = _assign_new_key(camera_id)
        cam = camera_db.get_camera(camera_id)
        # The setup details come back with the creation response (shown once on the
        # setup screen) and can be fetched again later through /rtmp-setup.
        try:
            cam["rtmp_setup"] = {"camera_id": camera_id, **ingest.setup_for(key)}
        except ingest.NotConfigured as e:
            cam["rtmp_setup"], cam["rtmp_setup_error"] = None, str(e)
    return cam


class StreamTestIn(BaseModel):
    rtsp_url: str | None = None      # original field, still accepted (RTSP)
    url: str | None = None           # either protocol
    protocol: Literal["rtsp", "rtmp"] | None = None


@app.post("/api/cameras/test-stream")
def test_camera_stream(payload: StreamTestIn, _: dict = Depends(auth.require_admin)):
    """Connects to an RTSP or RTMP link once and returns a single still (JPEG), so
    the Add Camera form can show the link works before it's saved. Runs in
    FastAPI's threadpool (plain def), so a slow camera never blocks others. The
    link is never echoed back in an error or written to the log."""
    url = (payload.url or payload.rtsp_url or "").strip()
    protocol = payload.protocol or ("rtmp" if url.lower().startswith(("rtmp://", "rtmps://")) else "rtsp")
    if protocol == "rtmp":
        try:
            url = stream_source.validate_rtmp_url(url)
        except stream_source.SourceError as e:
            raise HTTPException(status_code=422, detail=str(e))
    elif not url.lower().startswith("rtsp://"):
        raise HTTPException(status_code=422, detail="The link must start with rtsp://")
    cap = rtsp_reader.open_capture(url, 8.0, 8.0)
    try:
        if not cap.isOpened():
            raise HTTPException(status_code=400, detail="Couldn't connect. Check the address, port, username and password.")
        ok, frame = cap.read()
        if not ok or frame is None:
            raise HTTPException(status_code=400, detail="Connected, but the camera sent no video. Check the stream path.")
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
    finally:
        cap.release()
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@app.put("/api/cameras/{camera_id}")
def update_camera(camera_id: int, payload: CameraUpdate, principal: dict = Depends(auth.require_admin)):
    if camera_db.get_camera(camera_id) is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    fields = payload.model_dump(exclude_unset=True)
    sent = set(fields)                                      # what the user changed, for the audit entry
    existing = camera_db.get_camera_connection(camera_id) or {}
    if "protocol" in fields or "source_url" in fields:
        protocol = fields.get("protocol") or stream_source.protocol_of(existing)
        if protocol == stream_source.PUSH_PROTOCOL:
            fields.pop("source_url", None)
            fields.update(_push_fields())
        elif protocol == "rtmp":
            new_link = (fields.pop("source_url", None) or "").strip()
            if new_link:                                  # a new link replaces the stored one
                fields.update(_rtmp_fields(new_link))
            elif not (existing.get("source_url") or "").strip():
                raise HTTPException(status_code=422, detail="Enter the RTMP link, for example rtmp://host/app/stream-key.")
            # blank link on an RTMP camera that has one = unchanged (the form never
            # shows the stored link, so blank means "keep it")
            fields["protocol"] = "rtmp"
        else:
            fields["protocol"] = "rtsp"
            fields["source_url"] = ""
    # Reconnect only if how the camera is read actually changes: the edit form resends
    # the protocol and address on every save, and renaming a camera must not drop its video.
    reconnect = any(k in fields and fields[k] != existing.get(k) for k in _CONNECTION_FIELDS)
    camera_db.update_camera(camera_id, **fields)
    was_push = stream_source.protocol_of(existing) == stream_source.PUSH_PROTOCOL
    is_push = fields.get("protocol", stream_source.protocol_of(existing)) == stream_source.PUSH_PROTOCOL
    if is_push and not existing.get("rtmp_stream_key"):
        _assign_new_key(camera_id)
        reconnect = True
    elif was_push and not is_push and existing.get("rtmp_stream_key"):
        # No longer a push camera: its key stops working and its publisher is dropped.
        camera_db.set_stream_key(camera_id, None)
        ingest.kick_publishers(existing["rtmp_stream_key"])
    audit.record("camera.update", principal, target=f"camera:{camera_id}",
                 fields=sorted(k for k in sent if k not in ("password", "source_url")),
                 password_changed="password" in sent, source_changed="source_url" in sent and reconnect)
    if "live_feed_enabled" in fields:
        stream = camera_stream.get_stream(camera_id)
        stream.resume() if fields["live_feed_enabled"] else stream.stop()
        # Background keep-alives (footfall, desks, staff count) pick it up now,
        # not in 15 s. Each is independent; one failing is logged and the
        # others still run (it will also catch up on its own periodic sync).
        for name, sync in (("footfall", footfall.service._sync_gates), ("desks", desks.service._sync),
                           ("staff", staff_service.sync)):
            try:
                sync()
            except Exception:
                log.exception("camera %s: %s resync after feed change failed", camera_id, name)
    if reconnect and "live_feed_enabled" not in fields:
        camera_stream.get_stream(camera_id).restart()    # reconnect with the new details
    return camera_db.get_camera(camera_id)


# Setup details and key rotation are admin-only and rate-limited per admin.
_rtmp_setup_limiter = SlidingWindowLimiter(60, 60.0)
_rtmp_rotate_limiter = SlidingWindowLimiter(5, 600.0)


def _limit(limiter: SlidingWindowLimiter, principal: dict) -> None:
    who = str(principal.get("admin_user_id") or principal.get("token") or "admin")
    wait = limiter.retry_after(who)
    if wait > 0:
        raise HTTPException(status_code=429, detail="Too many requests, try again shortly",
                            headers={"Retry-After": str(int(wait) + 1)})
    limiter.hit(who)


def _push_camera(camera_id: int) -> dict:
    cam = camera_db.get_camera_connection(camera_id)
    if cam is None:
        raise HTTPException(status_code=404, detail="Camera not found")
    if stream_source.protocol_of(cam) != stream_source.PUSH_PROTOCOL or not cam.get("rtmp_stream_key"):
        raise HTTPException(status_code=404, detail="This camera does not use RTMP push")
    return cam


@app.get("/api/cameras/{camera_id}/rtmp-setup")
def get_rtmp_setup(camera_id: int, principal: dict = Depends(auth.require_admin)):
    """RTMP server, stream key and one-field URL for an RTMP-push camera, plus
    whether its video is actually arriving (Online only on real frames)."""
    _limit(_rtmp_setup_limiter, principal)
    cam = _push_camera(camera_id)
    out = _setup_payload(camera_id, cam["rtmp_stream_key"])
    out.update(ingest.stream_status(camera_id, cam["rtmp_stream_key"], camera_stream.get_stream(camera_id)))
    return out


@app.post("/api/cameras/{camera_id}/rtmp-key/rotate")
def rotate_rtmp_key(camera_id: int, principal: dict = Depends(auth.require_admin)):
    """New stream key for a camera whose key may be compromised. The old key
    stops working at once and anyone still publishing with it is disconnected;
    the camera must be given the new key."""
    _limit(_rtmp_rotate_limiter, principal)
    cam = _push_camera(camera_id)
    old = cam["rtmp_stream_key"]
    key = _assign_new_key(camera_id)
    ingest.kick_publishers(old)
    camera_stream.get_stream(camera_id).restart()
    audit.record("camera.rtmp_key_rotated", principal, target=f"camera:{camera_id}")
    return _setup_payload(camera_id, key)


@app.post("/api/ingest/auth")
async def ingest_auth(request: Request, secret: str | None = None):
    """Called by the ingest server for every publish/read. 200 = allow, 401 = deny.
    Authenticated by a shared secret, not a user session; never logs a key."""
    if not ingest.hook_secret_ok(secret):
        raise HTTPException(status_code=401, detail="denied")
    try:
        req = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="bad request")
    if not isinstance(req, dict):
        raise HTTPException(status_code=400, detail="bad request")
    d = await asyncio.to_thread(ingest.decide, req)
    if req.get("action") in ("publish", "read"):
        log.info("ingest %s %s from %s: %s%s", req.get("action"), "allowed" if d.allowed else "denied",
                 req.get("ip") or "?", d.reason, f" (camera {d.camera_id})" if d.camera_id else "")
    if not d.allowed:
        raise HTTPException(status_code=401, detail="denied")
    if req.get("action") == "publish" and d.camera_id is not None:
        # Start reading the camera's stream now (playback + AI), see ingest.ensure_reading.
        await asyncio.to_thread(ingest.ensure_reading, d.camera_id)
    return {"ok": True}


@app.delete("/api/cameras/{camera_id}")
def delete_camera(camera_id: int, principal: dict = Depends(auth.require_admin)):
    camera_db.delete_camera(camera_id)
    # Stop its read loop and reader process now, not at the next restart.
    camera_stream.remove_stream(camera_id)
    ingest.forget(camera_id)
    _report_cameras_soon()
    audit.record("camera.delete", principal, target=f"camera:{camera_id}")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------

class SiteIn(BaseModel):
    name: str
    description: str = ""


class SiteUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


@app.get("/api/sites")
def list_sites(principal: dict = Depends(auth.get_principal)):
    # Sites aren't owned by a client, but each site lists its cameras, so a
    # client only sees their own licensed cameras inside it (and only the
    # sites that contain one) — never another tenant's camera names.
    sites = camera_db.list_sites()
    allowed = auth.allowed_camera_ids(principal)
    if allowed is None:
        return sites
    scoped = []
    for s in sites:
        cams = [c for c in s.get("cameras", []) if c.get("id") in allowed]
        if cams:
            scoped.append({**s, "cameras": cams, "active_count": sum(1 for c in cams if c.get("status") == "active")})
    return scoped


@app.post("/api/sites")
def create_site(payload: SiteIn, _: dict = Depends(auth.require_admin)):
    camera_db.add_site(payload.name, payload.description)
    return {"ok": True}


@app.put("/api/sites/{site_id}")
def update_site(site_id: int, payload: SiteUpdate, _: dict = Depends(auth.require_admin)):
    camera_db.update_site(site_id, name=payload.name, description=payload.description)
    return {"ok": True}


@app.delete("/api/sites/{site_id}")
def delete_site(site_id: int, _: dict = Depends(auth.require_admin)):
    camera_db.delete_site(site_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Dashboard support: stats / alerts / settings / auth
# ---------------------------------------------------------------------------

_settings = {"detection_fps": 1.0}


class SettingsIn(BaseModel):
    # Only known keys, each bounded: this used to accept (and store) any
    # JSON object from anyone, unauthenticated.
    model_config = ConfigDict(extra="forbid")
    detection_fps: float = Field(ge=0.1, le=30)


@app.get("/api/stats")
def get_stats(_: dict = Depends(auth.require_admin)):
    cameras = camera_db.list_cameras()
    return {
        "total_cameras": len(cameras),
        "active_cameras": sum(1 for c in cameras if c["status"] == "active"),
        "faces_enrolled": 0,
        "active_alerts": 0,
        "detections_today": 0,
    }


@app.get("/api/settings")
def get_settings(_: dict = Depends(auth.require_admin)):
    return _settings


@app.put("/api/settings")
def update_settings(payload: SettingsIn, principal: dict = Depends(auth.require_admin)):
    changes = payload.model_dump()
    _settings.update(changes)
    audit.record("settings.update", principal, target="settings", **changes)
    return _settings


class LoginIn(BaseModel):
    email: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=1024)
    # Portal mode: the ticket POST /api/auth/sso gave for a Deco Vision pass.
    portal_ticket: str | None = Field(default=None, max_length=128)


@app.post("/api/auth/login")
def login(payload: LoginIn, request: Request):
    """Admin login: email + password checked against admin_users
    (auth.authenticate_admin). Failures are throttled per IP and per email
    (ratelimit.py) and always get the same generic message, whether the
    email exists or not."""
    ip = ratelimit.client_ip(request)
    guard = ratelimit.admin_login_guard
    guard.check(ip, payload.email)
    if master_link.enabled():
        # Portal mode: the license must allow access, and the form must come
        # from a Deco Vision pass (its ticket); the pass alone logs no one in.
        problem = master_link.license_problem()
        if problem:
            raise HTTPException(status_code=403, detail=problem)
        if not master_link.login_ticket_valid(payload.portal_ticket):
            guard.failed(ip, payload.email)
            raise HTTPException(status_code=401, detail="Open SMC from the Deco Vision portal to sign in.")
    admin = auth.authenticate_admin(payload.email, payload.password)
    if admin is None:
        guard.failed(ip, payload.email)
        if master_link.enabled():
            master_link.login_ticket_failed(payload.portal_ticket)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    guard.succeeded(ip, payload.email)
    if master_link.enabled():
        master_link.use_login_ticket(payload.portal_ticket)
    token = auth.create_admin_session(admin)
    audit.record("admin.login", {"email": admin["email"]}, ip=ip)
    return {"email": admin["email"], "name": admin["name"], "token": token}


@app.get("/api/auth/me")
def me(principal: dict = Depends(auth.require_admin)):
    # `license` is the Master Portal license summary (mode "standalone" when
    # SMC isn't connected); the frontend gates pages on its features.
    admin = auth.get_admin_user(principal["admin_user_id"])
    if admin is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"email": admin["email"], "name": admin["name"], "sso": False, "license": master_link.summary()}


@app.post("/api/auth/logout")
def logout(principal: dict = Depends(auth.get_principal)):
    auth.revoke_session(principal["token"])
    return {"ok": True}


@app.get("/api/audit")
def audit_log(limit: int = 200, _: dict = Depends(auth.require_admin)):
    return audit.recent(limit)


# ---------------------------------------------------------------------------
# Live video: one RTSP-reading thread per camera (camera_stream.py), fanned
# out to every connected viewer over its own websocket.
# ---------------------------------------------------------------------------

# Websockets are long-lived: the session/license is re-validated this often,
# so a logout, password change or license suspension ends an open live view.
_WS_REAUTH_SECONDS = 30.0


def _authorize_camera_ws(token: str | None, camera_id: int) -> bool:
    """Shared by both camera websockets below. Browsers can't attach an
    Authorization header to a WebSocket handshake, so the frontend passes
    the same bearer token as a `?token=` query param instead (see
    useLiveCameraFeed.js) — auth.get_session() is the same lookup
    get_principal() uses for HTTP requests, just invoked directly since
    there's no Header-based Depends() for this transport. Returns False
    (caller closes the socket) for: missing/invalid/expired token, a
    client whose license is no longer active, or a client whose license
    doesn't include this specific camera. An admin session may access any
    camera, matching the HTTP camera endpoints' behavior."""
    return auth.can_access_camera(auth.get_session(token), camera_id)


@app.websocket("/ws/live/{camera_id}")
async def ws_live(websocket: WebSocket, camera_id: int, token: str | None = None, plain: bool = False, w: int | None = None):
    await websocket.accept()
    if not _authorize_camera_ws(token, camera_id):
        await websocket.close(code=4401)
        return
    cam = camera_db.get_camera(camera_id)
    if not cam or not cam.get("host"):
        await websocket.close()
        return
    if not cam.get("live"):
        await websocket.close(code=4403, reason="Feed switched off")
        return

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)

    class ThreadSafePut:
        @staticmethod
        def put_nowait(item):
            loop.call_soon_threadsafe(_drop_oldest_and_put, queue, item)

    stream = camera_stream.get_stream(camera_id)
    # plain=1 (the plain Live Feed page, no boxes drawn) subscribes like a
    # background collector, so it doesn't make face_pipeline run the extra
    # person-overlay detection that only the AI Analytics view displays.
    # w: send frames scaled to this width (grid tiles ask for 960 px; see
    # camera_stream's encoding comment for the bandwidth this saves).
    stream.subscribe(ThreadSafePut, is_collector=plain, width=max(320, min(w, 3840)) if w else None)
    next_auth_check = loop.time() + _WS_REAUTH_SECONDS
    try:
        while True:
            try:
                frame = await asyncio.wait_for(queue.get(), timeout=_WS_REAUTH_SECONDS)
            except asyncio.TimeoutError:
                frame = None  # no video right now; still re-check the session below
            if loop.time() >= next_auth_check:
                next_auth_check = loop.time() + _WS_REAUTH_SECONDS
                if not await asyncio.to_thread(_authorize_camera_ws, token, camera_id):
                    await websocket.close(code=4401)
                    return
            if frame is not None:
                await websocket.send_bytes(frame)
    except WebSocketDisconnect:
        pass
    finally:
        stream.unsubscribe(ThreadSafePut)


def _drop_oldest_and_put(queue: asyncio.Queue, item: bytes) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        pass


@app.websocket("/ws/detections/{camera_id}")
async def ws_detections(websocket: WebSocket, camera_id: int, token: str | None = None):
    """Live PERSON overlay for LiveCameraTile/CameraViewerModal — reads
    face_pipeline.CameraFacePipeline.get_live_detections(), which is now
    person-track-based (full-body box, person-first — see
    _update_person_overlay): a person is listed here regardless of whether
    their face is currently visible/recognized. `employee_id`/`name` are
    only set once face recognition inside that person's box is confident
    AND temporally stable; otherwise the frontend shows "Person", never
    "Unknown". Populated by the same pipeline that already processes this
    camera's frames for /ws/live — no second inference path. fire_smoke
    stays [] — no such detector exists in this codebase. Authorized the
    same way as /ws/live."""
    await websocket.accept()
    if not _authorize_camera_ws(token, camera_id):
        await websocket.close(code=4401)
        return
    loop = asyncio.get_event_loop()
    next_auth_check = loop.time() + _WS_REAUTH_SECONDS
    try:
        while True:
            if loop.time() >= next_auth_check:
                next_auth_check = loop.time() + _WS_REAUTH_SECONDS
                if not await asyncio.to_thread(_authorize_camera_ws, token, camera_id):
                    await websocket.close(code=4401)
                    return
            pipeline = face_pipeline.get_existing_pipeline(camera_id)
            people = []
            if pipeline is not None:
                for det in pipeline.get_live_detections():
                    emp_id = det["employee_id"]
                    # Color is resolved from employee_id -> company via
                    # employee_directory.py, never guessed from the display
                    # name. This dict is rebuilt field-by-field from
                    # get_live_detections() rather than forwarded as-is, so
                    # "color" (and face_pipeline.py's own "name") were
                    # previously dropped here even though face_pipeline.py
                    # already computed them — that's what made every box
                    # render in the frontend's gray fallback regardless of
                    # employee_id.
                    _, color = employee_directory.get_display(emp_id)
                    people.append({
                        "track_id": det["track_id"],
                        "bbox": det["bbox"],
                        "employee_id": emp_id,
                        "name": _employee_display_name(emp_id) if emp_id else None,
                        "color": color,
                        "face_bbox": det.get("face_bbox"),
                        "confidence": det["confidence"],
                        # face | appearance (named by body, appearance.py) | None
                        "identity_source": det.get("identity_source"),
                        # Additive fields — every existing consumer of this
                        # payload keeps working unchanged. Both are null/0
                        # when the expression model has nothing confident
                        # for this track, which never affects the name.
                        "expression": det.get("expression"),
                        "expression_confidence": det.get("expression_confidence", 0.0),
                    })
            # Boxes are in the camera's full-resolution pixels; the frame
            # size lets a viewer showing a scaled-down picture place them.
            # If recognition has not produced an update for a while (CPU-bound, stalled),
            # showing its last boxes would pretend they are current: send none instead.
            # (Boxes are no longer blanked wholesale when the pipeline is slow: every
            # box expires on its own clock inside get_live_detections(), so a busy
            # pipeline shows a small "delayed" badge instead of a blinking overlay.)
            fw, fh = pipeline.frame_size if pipeline is not None and pipeline.frame_size else (None, None)
            await websocket.send_json({
                "people": people, "fire_smoke": [], "frame_w": fw, "frame_h": fh,
                # Freshness, so a viewer can say "recognition delayed" instead of showing stale boxes as live.
                "updated_at": pipeline.last_overlay_at if pipeline is not None else None,
                "server_time": time.time(),
            })
            await asyncio.sleep(0.3)
    except (WebSocketDisconnect, RuntimeError):   # RuntimeError: browser closed mid-send
        pass


def _employee_display_name(employee_id: str) -> str:
    """Resolves a classifier's predicted employee_id to a real name from
    the SAME roster face-training validates labels against (face_db's
    employees table) — never invents a name; falls back to the bare ID if
    that roster doesn't (yet) have an entry for it.

    Cached for 30 s: this runs for every person in every overlay push
    (~3/s per open viewer), which used to be a full-table query each time."""
    global _names_cache, _names_cached_at
    now = time.monotonic()
    if now - _names_cached_at > 30:
        _names_cache = {e["employee_id"]: e["name"] for e in face_db.list_employees()}
        _names_cached_at = now
    return _names_cache.get(employee_id, employee_id)


_names_cache: dict[str, str] = {}
_names_cached_at = -1e9
