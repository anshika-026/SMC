"""
config.py

Deployment settings for the SMC unique-footfall service. Every value comes
from the environment (optionally via backend/.env) with a default that is
safe for local development. Re-ID tuning knobs live in app/reid/config.py.
"""

import os
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

BACKEND_DIR = Path(__file__).resolve().parent.parent

try:
    from dotenv import load_dotenv

    # Real environment variables win over .env, so a systemd
    # EnvironmentFile or CI secret is never shadowed by a stale file.
    load_dotenv(BACKEND_DIR / ".env", override=False)
except ImportError:
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw in (None, "") else float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw in (None, "") else int(raw)


def _path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    return Path(raw).expanduser().resolve() if raw else default


# --- Paths -------------------------------------------------------------------
DATA_DIR = _path("DATA_DIR", BACKEND_DIR / "data")
DB_PATH = _path("DB_PATH", DATA_DIR / "smc_footfall.db")
MODEL_DIR = _path("MODEL_DIR", BACKEND_DIR / "models")
# On: never let torchreid silently download an ImageNet backbone (which
# over-counts badly) — fail loudly until fetch_reid_model has been run.
MODEL_OFFLINE_MODE = _bool("MODEL_OFFLINE_MODE", False)

# --- API ---------------------------------------------------------------------
# Optional shared secret. When set, every /api/footfall/* call needs
# "Authorization: Bearer <API_TOKEN>". Empty = open (local/LAN testing).
API_TOKEN = os.getenv("API_TOKEN", "")
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if o.strip()]

# --- Cameras -----------------------------------------------------------------
# The SMC deployment counts on exactly these feeds. Credentials are kept out
# of the URL (CAMERA_n_USERNAME / CAMERA_n_PASSWORD) and percent-encoded here,
# because the password may contain "@", which breaks a hand-written RTSP URL.
CAMERA_SLOTS = (1, 2)
RTSP_TRANSPORT = os.getenv("RTSP_TRANSPORT", "tcp")
RTSP_RECONNECT_BASE_SECONDS = _float("RTSP_RECONNECT_BASE_SECONDS", 2.0)
RTSP_RECONNECT_MAX_SECONDS = _float("RTSP_RECONNECT_MAX_SECONDS", 60.0)

# --- Recovery (used by resilience.py) -------------------------------------------
FEATURE_RETRY_BASE_SECONDS = _float("FEATURE_RETRY_BASE_SECONDS", 5.0)
FEATURE_RETRY_MAX_SECONDS = _float("FEATURE_RETRY_MAX_SECONDS", 300.0)
FEATURE_REBUILD_AFTER_FAILURES = _int("FEATURE_REBUILD_AFTER_FAILURES", 3)


def with_credentials(url: str, username: str, password: str) -> str:
    """url with user/password inserted (percent-encoded). A URL that already
    carries credentials is returned unchanged when none are given."""
    if not username:
        return url
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    auth = quote(username, safe="") + (":" + quote(password, safe="") if password else "")
    return urlunsplit((parts.scheme, f"{auth}@{host}", parts.path, parts.query, parts.fragment))


def cameras() -> list[dict]:
    """The configured feeds: [{id, name, url}], skipping empty slots."""
    out = []
    for n in CAMERA_SLOTS:
        url = os.getenv(f"CAMERA_{n}_URL", "").strip()
        if not url:
            continue
        out.append({
            "id": n,
            "name": os.getenv(f"CAMERA_{n}_NAME", f"Camera {n}"),
            "url": with_credentials(url, os.getenv(f"CAMERA_{n}_USERNAME", ""), os.getenv(f"CAMERA_{n}_PASSWORD", "")),
        })
    return out


def redact(url: str) -> str:
    """url with the password hidden, for logs and the API."""
    parts = urlsplit(url)
    if "@" not in parts.netloc:
        return url
    auth, host = parts.netloc.rsplit("@", 1)
    user = auth.split(":", 1)[0]
    return urlunsplit((parts.scheme, f"{user}:***@{host}", parts.path, parts.query, parts.fragment))
