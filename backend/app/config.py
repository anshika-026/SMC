"""
config.py

The one place deployment-specific settings are read. Every value comes from
the environment (optionally via backend/.env, loaded below) with a default
that is safe for local development. `validate()` runs at startup and refuses
to boot a production deployment that is missing something it cannot run
safely without (see the checks at the bottom).

Feature-tuning knobs that only one module cares about (face_pipeline's
thresholds, reid/config.py's Re-ID tuning) still live next to the code that
uses them; this module owns the cross-cutting ones: paths, auth, network
timeouts, recovery, retention, logging and external services.
"""

import os
import secrets
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    # Real environment variables win over .env (override=False), so a
    # systemd EnvironmentFile or CI secret is never shadowed by a stale file.
    load_dotenv(BACKEND_DIR / ".env", override=False)
except ImportError:
    pass


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from e


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"{name} must be a number, got {raw!r}") from e


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _path(name: str, default: Path) -> Path:
    raw = _str(name)
    p = Path(raw) if raw else default
    return p if p.is_absolute() else (BACKEND_DIR / p).resolve()


# --- Environment ------------------------------------------------------------
# development | production | test
APP_ENV = _str("APP_ENV", "development").lower()
IS_PRODUCTION = APP_ENV == "production"

# --- Server -------------------------------------------------------------------
HOST = _str("HOST", "127.0.0.1")
PORT = _int("PORT", 8822)   # SMC; 8821 is MAINDV's backend on the same machine

# Browser origins allowed to call this API / open websockets. Never "*":
# the API uses credentials, and a wildcard would let any site drive it.
CORS_ORIGINS = [o.strip() for o in _str("CORS_ORIGINS", "http://localhost:5180,http://127.0.0.1:5180").split(",") if o.strip()]

# Interactive /docs and /openapi.json. Handy locally, an API map for
# attackers in production.
ENABLE_API_DOCS = _bool("ENABLE_API_DOCS", not IS_PRODUCTION)

# --- Paths --------------------------------------------------------------------
# All runtime data (SQLite DB, face captures, snapshots, trained classifier)
# lives under DATA_DIR. Image paths are stored in the DB relative to it (see
# storage.py), so the whole directory can be moved or restored elsewhere.
DATA_DIR = _path("DATA_DIR", BACKEND_DIR / "data")
DB_PATH = _path("DB_PATH", DATA_DIR / "app.db")
MODEL_DIR = _path("MODEL_DIR", BACKEND_DIR / "models")

# --- Models -------------------------------------------------------------------
# Offline mode: never reach the internet for model weights. Every model must
# already be in MODEL_DIR / the InsightFace root / the HF cache (see
# scripts/fetch_models.py and DEPLOYMENT.md). On by default in production.
MODEL_OFFLINE_MODE = _bool("MODEL_OFFLINE_MODE", IS_PRODUCTION)

# AI features this deployment offers (keys of analytics_settings.FEATURES,
# comma-separated). A feature not listed never runs, can't be switched on,
# and isn't reported to the dashboard. Empty = every feature. The SMC
# deployment sets ENABLED_FEATURES=footfall.
ENABLED_FEATURES = frozenset(f.strip() for f in _str("ENABLED_FEATURES", "").split(",") if f.strip())
# InsightFace looks for <root>/models/buffalo_l. Defaults to its own default
# location so an existing developer install keeps working.
INSIGHTFACE_ROOT = _path("INSIGHTFACE_ROOT", Path.home() / ".insightface")
# Where face detection/recognition (InsightFace, onnxruntime) runs: "auto" uses
# the GPU when onnxruntime-gpu can (the GPU server), else the CPU (plain
# onnxruntime, e.g. the laptop); "cpu" always uses the CPU.
FACE_ONNX_DEVICE = _str("FACE_ONNX_DEVICE", "auto").lower()

# --- Live video ---------------------------------------------------------------
# How often the live-view websocket pushes a JPEG frame, independent of the
# camera's own frame rate — keeps bandwidth/CPU bounded regardless of source FPS.
LIVE_STREAM_FPS = _float("LIVE_STREAM_FPS", 8.0)

# --- RTSP / camera resilience -------------------------------------------------
RTSP_CONNECT_TIMEOUT = _float("RTSP_CONNECT_TIMEOUT", 20.0)     # seconds to open a stream (WAN NVRs can be slow)
RTSP_READ_TIMEOUT = _float("RTSP_READ_TIMEOUT", 10.0)           # seconds a single read may block
RTSP_RECONNECT_DELAY = _float("RTSP_RECONNECT_DELAY", 2.0)      # first retry delay
RTSP_RECONNECT_MAX_DELAY = _float("RTSP_RECONNECT_MAX_DELAY", 60.0)  # backoff ceiling
# Consecutive failed connects before a camera is reported FAILED (it keeps
# retrying at the max delay; this only changes the reported state).
RTSP_MAX_RETRIES = _int("RTSP_MAX_RETRIES", 10)

# --- RTMP push ingest (cameras that stream TO us; see ingest.py) -------------------
# What a camera is told to publish to. RTMP_PUBLIC_HOST must be the address the
# CAMERAS can reach (LAN IP or public DNS name). Left empty, the setup screen
# says RTMP push is not configured instead of showing a made-up address.
RTMP_PUBLIC_HOST = _str("RTMP_PUBLIC_HOST", "")
RTMP_PUBLIC_PORT = _int("RTMP_PUBLIC_PORT", 1935)
# rtmps only once the ingest server has a TLS certificate (see mediamtx.yml).
RTMP_PUBLIC_SCHEME = _str("RTMP_PUBLIC_SCHEME", "rtmp").lower()
RTMP_APP = _str("RTMP_APP", "live").strip("/")
# How the one-field "full URL" is assembled; {server} and {key} are substituted.
# Most devices take server + "/" + key; a few want e.g. "{server}?key={key}".
RTMP_FULL_URL_FORMAT = _str("RTMP_FULL_URL_FORMAT", "{server}/{key}")
# Where the backend READS pushed streams from (the ingest server's internal RTSP
# side), with the read-only account the auth hook accepts.
INGEST_RTSP_BASE = _str("INGEST_RTSP_BASE", "rtsp://mediamtx:8554").rstrip("/")
INGEST_API_URL = _str("INGEST_API_URL", "http://mediamtx:9997").rstrip("/")
INGEST_READ_USER = _str("INGEST_READ_USER", "smc-reader")
INGEST_READ_PASSWORD = _str("INGEST_READ_PASSWORD", "")
# Shared secret the ingest server sends with every auth-hook call.
INGEST_HOOK_SECRET = _str("INGEST_HOOK_SECRET", "")
# No new frame for this long = the reader is stalled; the watchdog restarts it.
CAMERA_FRAME_TIMEOUT = _float("CAMERA_FRAME_TIMEOUT", 20.0)
CAMERA_WATCHDOG_INTERVAL = _float("CAMERA_WATCHDOG_INTERVAL", 2.0)

# --- Analytics feature recovery ------------------------------------------------
# After an analytics component (face recognition, person detection, ...)
# throws, it is retried after an exponentially growing pause instead of
# being switched off for good.
FEATURE_RETRY_BASE_SECONDS = _float("FEATURE_RETRY_BASE_SECONDS", 5.0)
FEATURE_RETRY_MAX_SECONDS = _float("FEATURE_RETRY_MAX_SECONDS", 300.0)
# Consecutive failures after which the component is rebuilt from scratch
# (models reloaded) rather than just retried.
FEATURE_REBUILD_AFTER_FAILURES = _int("FEATURE_REBUILD_AFTER_FAILURES", 3)

# --- Auth -----------------------------------------------------------------------
SESSION_TTL_SECONDS = _int("SESSION_TTL_HOURS", 12) * 3600
PASSWORD_MIN_LENGTH = _int("PASSWORD_MIN_LENGTH", 10)
# One-time bootstrap: if no admin account exists yet, create this one at
# startup. Remove ADMIN_PASSWORD from the environment afterwards.
ADMIN_EMAIL = _str("ADMIN_EMAIL")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# Login throttling (ratelimit.py), per rolling window.
LOGIN_RATE_WINDOW_SECONDS = _int("LOGIN_RATE_WINDOW_SECONDS", 300)
LOGIN_RATE_LIMIT_PER_IP = _int("LOGIN_RATE_LIMIT_PER_IP", 20)
LOGIN_RATE_LIMIT_PER_USER = _int("LOGIN_RATE_LIMIT_PER_USER", 5)
# Honour X-Forwarded-For only when a trusted reverse proxy sets it.
TRUST_PROXY_HEADERS = _bool("TRUST_PROXY_HEADERS", IS_PRODUCTION)

# Used to sign the license QR payload (license_qr.py). Must be fixed in
# production, or every issued QR stops verifying after a restart.
JWT_SECRET = os.environ.get("JWT_SECRET") or secrets.token_hex(32)
JWT_SECRET_IS_EPHEMERAL = not os.environ.get("JWT_SECRET")
JWT_ALGORITHM = "HS256"

# --- Uploads --------------------------------------------------------------------
MAX_UPLOAD_BYTES = _int("MAX_UPLOAD_MB", 5) * 1024 * 1024

# --- External services ------------------------------------------------------------
# The separately deployed face-enrollment ("Identity") service the People page
# and gallery sync read from. Empty = those features report "not configured".
IDENTITY_SERVICE_BASE = _str("IDENTITY_SERVICE_BASE").rstrip("/")
EXTERNAL_HTTP_TIMEOUT = _float("EXTERNAL_HTTP_TIMEOUT", 15.0)

# --- Deco Vision Master Portal (master_link.py) -------------------------------------
# "standalone" (default): SMC works exactly as before, nothing below is used.
# "master": clients sign in on the Master Portal and arrive with a signed launch
# token; the license there decides access, camera limit and features.
INTEGRATION_MODE = _str("INTEGRATION_MODE", "standalone").lower()
# Server-to-server address of the Master Portal API (from inside Docker on a
# laptop that is http://host.docker.internal:8821, in production https://...).
MASTER_PORTAL_URL = _str("MASTER_PORTAL_URL").rstrip("/")
# Where a browser signs in (the license's client login page). Logout and an
# expired session go back here.
MASTER_PORTAL_LOGIN_URL = _str("MASTER_PORTAL_LOGIN_URL")
# The license this deployment belongs to; a launch token for any other license
# is refused.
MASTER_LICENSE_ID = _str("MASTER_LICENSE_ID")
# The license's application key (secret; the Master Portal shows it once).
MASTER_APP_API_KEY = os.environ.get("MASTER_APP_API_KEY", "")
# The Master Portal's PUBLIC signing key: base64 of the raw Ed25519 key (one
# line), or a PEM. Pinned here on purpose, never fetched at runtime.
MASTER_PORTAL_PUBLIC_KEY = os.environ.get("MASTER_PORTAL_PUBLIC_KEY", "").replace("\\n", "\n").strip()
ENTITLEMENT_REFRESH_SECONDS = _float("ENTITLEMENT_REFRESH_SECONDS", 60.0)
# If the Master Portal can't be reached, keep using the last license read for
# this long, then lock until it is reachable again.
ENTITLEMENT_GRACE_HOURS = _float("ENTITLEMENT_GRACE_HOURS", 24.0)

# --- Retention (retention.py) -------------------------------------------------------
# Master switch for the hourly cleanup (expired sessions are always purged).
RETENTION_ENABLED = _bool("RETENTION_ENABLED", True)
# Deleting image files no database row points at is opt-in: such files can be
# data restored or copied in by hand, and deleting them is irreversible.
RETENTION_DELETE_ORPHANS = _bool("RETENTION_DELETE_ORPHANS", False)
RETENTION_INTERVAL_SECONDS = _int("RETENTION_INTERVAL_SECONDS", 3600)
# Unassigned review-queue captures (face_pending) and their images.
FACE_PENDING_RETENTION_DAYS = _int("FACE_PENDING_RETENTION_DAYS", 30)
# Unlabeled / rejected / no-embedding training captures. Labeled and skipped
# captures are the training set and are never deleted by retention.
FACE_CAPTURE_RETENTION_DAYS = _int("FACE_CAPTURE_RETENTION_DAYS", 60)
# Re-ID body snapshots (footfall UAT panel).
SNAPSHOT_RETENTION_DAYS = _int("SNAPSHOT_RETENTION_DAYS", 30)
# Snapshots attached to alerts that have been resolved.
ALERT_SNAPSHOT_RETENTION_DAYS = _int("ALERT_SNAPSHOT_RETENTION_DAYS", 90)
# Evidence clips of intrusion / loitering alerts (evidence.py): removed after this many days, the alert stays.
ALERT_CLIP_RETENTION_DAYS = _int("ALERT_CLIP_RETENTION_DAYS", 7)
# Classifier backups (classifier.joblib.bak-*) kept besides the active model.
MODEL_BACKUP_RETENTION_COUNT = _int("MODEL_BACKUP_RETENTION_COUNT", 5)

# --- Logging ------------------------------------------------------------------------
LOG_LEVEL = _str("LOG_LEVEL", "INFO").upper()
LOG_DIR = _path("LOG_DIR", BACKEND_DIR / "logs")
LOG_TO_FILE = _bool("LOG_TO_FILE", IS_PRODUCTION)
LOG_FILE_MAX_BYTES = _int("LOG_FILE_MAX_MB", 20) * 1024 * 1024
LOG_BACKUP_COUNT = _int("LOG_BACKUP_COUNT", 10)


def validate() -> list[str]:
    """Problems with the current configuration. In production any problem is
    fatal (main.py refuses to start); in development they are logged as
    warnings so a fresh checkout still runs."""
    problems = []
    if APP_ENV not in ("development", "production", "test"):
        problems.append(f"APP_ENV must be development, production or test (got {APP_ENV!r})")
    if "*" in CORS_ORIGINS:
        problems.append("CORS_ORIGINS must list explicit origins, never '*'")
    if IS_PRODUCTION:
        if JWT_SECRET_IS_EPHEMERAL:
            problems.append("JWT_SECRET is not set (license QR codes would stop verifying on every restart)")
        elif len(JWT_SECRET) < 32:
            problems.append("JWT_SECRET must be at least 32 characters")
        if not MODEL_OFFLINE_MODE:
            problems.append("MODEL_OFFLINE_MODE must be true in production (no runtime model downloads)")
        if IDENTITY_SERVICE_BASE.startswith("http://"):
            problems.append("IDENTITY_SERVICE_BASE must use https:// in production")
        if any(o.startswith("http://") and "localhost" not in o and "127.0.0.1" not in o for o in CORS_ORIGINS):
            problems.append("CORS_ORIGINS must use https:// origins in production")
    if INTEGRATION_MODE not in ("standalone", "master"):
        problems.append(f"INTEGRATION_MODE must be standalone or master (got {INTEGRATION_MODE!r})")
    if INTEGRATION_MODE == "master":
        for name in ("MASTER_PORTAL_URL", "MASTER_PORTAL_LOGIN_URL", "MASTER_LICENSE_ID", "MASTER_APP_API_KEY", "MASTER_PORTAL_PUBLIC_KEY"):
            if not globals()[name]:
                problems.append(f"{name} must be set when INTEGRATION_MODE=master")
        if IS_PRODUCTION and MASTER_PORTAL_URL.startswith("http://"):
            problems.append("MASTER_PORTAL_URL must use https:// in production")
    if PASSWORD_MIN_LENGTH < 8:
        problems.append("PASSWORD_MIN_LENGTH must be at least 8")
    if RTSP_RECONNECT_DELAY <= 0 or RTSP_RECONNECT_MAX_DELAY < RTSP_RECONNECT_DELAY:
        problems.append("RTSP_RECONNECT_DELAY must be > 0 and <= RTSP_RECONNECT_MAX_DELAY")
    if FEATURE_RETRY_BASE_SECONDS <= 0 or FEATURE_RETRY_MAX_SECONDS < FEATURE_RETRY_BASE_SECONDS:
        problems.append("FEATURE_RETRY_BASE_SECONDS must be > 0 and <= FEATURE_RETRY_MAX_SECONDS")
    return problems
