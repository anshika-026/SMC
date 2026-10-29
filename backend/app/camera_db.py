"""Camera + site registry, SQLite-backed. Mirrors the shape the frontend's
api.js already expects (listCameras/createCamera/... and listSites/...)."""

import sqlite3
from app import config, db, stream_source

DB_PATH = config.DB_PATH


def get_connection():
    """Shared connection policy (app/db.py): lock timeout + busy_timeout,
    commit on success, rollback on error, and always closed."""
    return db.connect(DB_PATH, row_factory=None)


def init_db() -> None:
    with get_connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cameras (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                site TEXT NOT NULL DEFAULT '',
                cam_code TEXT DEFAULT '',
                purpose TEXT DEFAULT 'GENERAL',
                host TEXT DEFAULT '',
                port INTEGER DEFAULT 554,
                user TEXT DEFAULT '',
                password TEXT DEFAULT '',
                stream_path TEXT DEFAULT '',
                vendor TEXT DEFAULT '',
                status TEXT DEFAULT 'inactive',
                live_feed_enabled INTEGER DEFAULT 1,
                attendance_tracking INTEGER DEFAULT 1,
                protocol TEXT DEFAULT 'rtsp',
                source_url TEXT DEFAULT '',
                rtmp_stream_key TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT ''
            )
            """
        )
        _migrate_cameras_table(conn)
        _seed_sites_if_empty(conn)


def _migrate_cameras_table(conn: sqlite3.Connection) -> None:
    """CREATE TABLE IF NOT EXISTS doesn't add new columns to an
    already-existing table — this backfills them for databases created
    before attendance_tracking existed."""
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(cameras)").fetchall()}
    if "attendance_tracking" not in existing_cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN attendance_tracking INTEGER DEFAULT 1")
    # Source protocol. Additive: every existing row reads back as 'rtsp' and keeps
    # working from its host/port/user/password/stream_path exactly as before.
    # source_url holds an RTMP link (its stream key is a secret; see stream_source.py).
    if "protocol" not in existing_cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN protocol TEXT DEFAULT 'rtsp'")
    if "source_url" not in existing_cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN source_url TEXT DEFAULT ''")
    # RTMP push: the camera-specific publish key (secret, unique, NULL for every
    # other camera). Additive; existing rows get NULL.
    if "rtmp_stream_key" not in existing_cols:
        conn.execute("ALTER TABLE cameras ADD COLUMN rtmp_stream_key TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_cameras_rtmp_stream_key ON cameras(rtmp_stream_key) "
                 "WHERE rtmp_stream_key IS NOT NULL")


def _seed_sites_if_empty(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM sites").fetchone()[0]
    if count > 0:
        return
    conn.execute("INSERT OR IGNORE INTO sites (name, description) VALUES (?, '')", ("Noida Site",))


def is_streamable(cam: dict | None) -> bool:
    """Has an address and its feed is switched on (Camera Management's
    Live feed switch). A switched-off camera is never connected to: no live
    view, no analytics, no background streaming. An RTMP camera also needs its
    RTMP link (rows from the API only say whether one is set: source_url_set)."""
    if not (cam and cam.get("host") and cam.get("live_feed_enabled", 1)):
        return False
    if stream_source.protocol_of(cam) == "rtmp":
        return bool(cam.get("source_url") or cam.get("source_url_set"))
    if stream_source.protocol_of(cam) == stream_source.PUSH_PROTOCOL:
        return bool(cam.get("rtmp_stream_key") or cam.get("rtmp_key_set"))
    return True


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["protocol"] = stream_source.protocol_of(d)
    d["rtmp_key_set"] = bool(d.get("rtmp_stream_key"))
    d["is_configured"] = bool(d["host"])
    d["live"] = is_streamable(d)
    # Status shown in the UI: a camera whose feed is switched off is inactive.
    d["status"] = "active" if d["live"] else "inactive"
    d.pop("password", None)  # never sent to the frontend
    # The RTMP link carries the stream key: only a masked form and a "set" flag
    # ever leave the backend, never the link itself.
    source = d.pop("source_url", "") or ""
    d["source_url_set"] = bool(source)
    # RTMP push key: never in camera listings (fetch it via the setup endpoint).
    d["rtmp_key_set"] = bool(d.pop("rtmp_stream_key", None))
    d["source_display"] = stream_source.redact_source(source) if source else ""
    return d


def list_cameras() -> list[dict]:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM cameras ORDER BY id").fetchall()
    return [_row_to_dict(r) for r in rows]


def get_camera(camera_id: int) -> dict | None:
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_camera_connection(camera_id: int) -> dict | None:
    """Includes the password and the RTMP link — internal use only (building the
    address the reader opens)."""
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM cameras WHERE id = ?", (camera_id,)).fetchone()
    return dict(row) if row else None


def add_camera(name: str, site: str, **fields) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO cameras
               (name, site, cam_code, purpose, host, port, user, password, stream_path, vendor,
                status, live_feed_enabled, attendance_tracking, protocol, source_url, rtmp_stream_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                site,
                fields.get("cam_code", ""),
                fields.get("purpose", "GENERAL"),
                fields.get("host", ""),
                fields.get("port", 554),
                fields.get("user", ""),
                fields.get("password", ""),
                fields.get("stream_path", "/h264/ch1/sub/av_stream"),
                fields.get("vendor", ""),
                "active" if fields.get("host") else "inactive",
                int(fields.get("live_feed_enabled", True)),
                int(fields.get("attendance_tracking", True)),
                fields.get("protocol") or "rtsp",
                fields.get("source_url") or "",
                fields.get("rtmp_stream_key") or None,
            ),
        )
        return cur.lastrowid


def update_camera(camera_id: int, **fields) -> None:
    if not fields:
        return
    if "host" in fields:
        fields["status"] = "active" if fields["host"] else "inactive"
    columns = ", ".join(f"{k} = ?" for k in fields)
    with get_connection() as conn:
        conn.execute(f"UPDATE cameras SET {columns} WHERE id = ?", (*fields.values(), camera_id))


def get_camera_by_stream_key(key: str) -> dict | None:
    """The RTMP-push camera that owns this publish key (internal: includes secrets)."""
    if not stream_source.is_stream_key(key):
        return None
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM cameras WHERE rtmp_stream_key = ? AND protocol = ?",
                           (key, stream_source.PUSH_PROTOCOL)).fetchone()
    return dict(row) if row else None


def set_stream_key(camera_id: int, key: str | None) -> None:
    """Store (or with None clear) a camera's publish key. The unique index makes
    a collision an IntegrityError, so a caller can retry with a fresh key."""
    with get_connection() as conn:
        conn.execute("UPDATE cameras SET rtmp_stream_key = ? WHERE id = ?", (key, camera_id))


def delete_camera(camera_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM cameras WHERE id = ?", (camera_id,))


def list_sites() -> list[dict]:
    cameras = list_cameras()
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM sites ORDER BY id").fetchall()
    result = []
    for r in rows:
        site = dict(r)
        site_cameras = [c for c in cameras if c["site"] == site["name"]]
        site["cameras"] = site_cameras
        site["active_count"] = sum(1 for c in site_cameras if c["status"] == "active")
        result.append(site)
    return result


def add_site(name: str, description: str = "") -> int:
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO sites (name, description) VALUES (?, ?)", (name, description))
        return cur.lastrowid


def update_site(site_id: int, name: str | None = None, description: str | None = None) -> None:
    with get_connection() as conn:
        if name is not None:
            old_name = conn.execute("SELECT name FROM sites WHERE id = ?", (site_id,)).fetchone()
            if old_name and old_name[0] != name:
                conn.execute("UPDATE cameras SET site = ? WHERE site = ?", (name, old_name[0]))
            conn.execute("UPDATE sites SET name = ? WHERE id = ?", (name, site_id))
        if description is not None:
            conn.execute("UPDATE sites SET description = ? WHERE id = ?", (description, site_id))


def delete_site(site_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM sites WHERE id = ?", (site_id,))
