"""
visitor_registry.py

Visitor RECORDS: who visited, why, whom they met, when they came in and left,
how long they waited and stayed, and their history across visits. It sits next
to visitors.py (the seating-area alerts), which it reads camera evidence from
but never changes.

Data model (four separate tables, foreign keys on):
  vm_visitors      permanent visitor PROFILES (Visitor ID "VIS-000001")
  vm_visits        one row per VISIT (Visit ID "VST-20261030-00001"): purpose,
                   host, consent, and the current entry / exit / host-notified /
                   meeting-start times. Name and company are copied in at
                   registration, so editing a profile never rewrites history.
  vm_visit_events  every timestamp ever recorded for a visit, with where it came
                   from (operator, camera, manual correction) and who did it.
                   A correction VOIDS the old event and adds a new one, so the
                   visit's times are always derivable and auditable.
  vm_visit_media   snapshot / clip files of a visit, each with its own expiry.

Identity: nothing here identifies a person from a camera. A camera detection
(a footfall gate entry/exit or a visitor-area arrival, already recorded by those
features) only becomes a visit's entry or exit when an operator LINKS it to that
visit. The operator can also simply check a visitor in/out at reception.
Returning visitors are matched by mobile / e-mail / name and the operator
confirms the match; no face matching, no embeddings.

Snapshots: only for visits whose visitor agreed (snapshot_consent). Frames come
from the linked detection's clip or a short live capture; the existing face
DETECTOR (detection only, never recognition) scores each face for size,
confidence and sharpness, and only the single best clear face is kept per visit.
No consent -> no snapshot and no clip, and nothing is captured "just in case".

Waiting time = meeting start - entry (never from registration).
Visit duration = exit - entry; still inside -> elapsed so far.
Both are None (shown as "not recorded") when a time is missing.
"""

from __future__ import annotations

import csv
import datetime
import difflib
import io
import logging
import re
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import cv2
import numpy as np

from app import audit, camera_db, config, db, employee_directory, lifecycle

log = logging.getLogger("visitor_registry")

DB_PATH = config.DB_PATH
MEDIA_DIR = Path(config.DATA_DIR) / "visitor_records"

PENDING, IN, OUT, CANCELLED = "pending", "in", "out", "cancelled"
TIME_KINDS = ("entry", "exit", "host_notified", "meeting_start")
FUTURE_SKEW_SECONDS = 120          # clock difference tolerated for "now" times
PURGE_INTERVAL_SECONDS = 3600

DEFAULT_SETTINGS = {
    "media_retention_days": 30,
    "long_stay_minutes": 120,
    "waiting_alert_minutes": 15,       # a checked-in visitor not yet met for longer than this needs attention
    "default_gate_camera_id": None,
    "notice_text": ("This building is monitored by CCTV. With your permission, one photo of you is taken "
                    "at entry and kept with this visit record for {days} days. You may decline; your visit "
                    "is registered either way."),
}

# Snapshot quality gates (pixels of the face box in the analysed frame).
MIN_FACE_CONF = 0.55
MIN_FACE_SIDE = 36
MIN_FACE_BLUR = 35.0
MIN_BRIGHTNESS, MAX_BRIGHTNESS = 35.0, 225.0
LIVE_CAPTURE_FRAMES = 4
CLIP_SNAPSHOT_FRAMES = 8     # frames of an entry clip checked for a face (the detector is slow on a busy CPU)
LIVE_CAPTURE_SECONDS = 8.0


def _conn():
    conn_cm = db.connect(DB_PATH, row_factory=sqlite3.Row)
    return _FkConn(conn_cm)


class _FkConn:
    """db.connect() with foreign keys switched on (so deleting a visitor
    removes their visits, events and media rows with it)."""

    def __init__(self, cm):
        self._cm = cm

    def __enter__(self):
        conn = self._cm.__enter__()
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


class RegistryError(ValueError):
    """A request that can't be done as asked (the message says why)."""


class DuplicateVisitor(RegistryError):
    def __init__(self, matches):
        super().__init__("This looks like a visitor who is already registered. Link the visit to their profile, or confirm it is a different person.")
        self.matches = matches


def init_db() -> None:
    with _conn() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS vm_visitors (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   visitor_code TEXT UNIQUE,
                   name TEXT NOT NULL,
                   name_key TEXT NOT NULL,          -- lower-case, single-spaced (duplicate check)
                   company TEXT,
                   mobile TEXT,
                   mobile_key TEXT,                 -- last 10 digits (duplicate check)
                   email TEXT,
                   notes TEXT,
                   created_at REAL NOT NULL,
                   updated_at REAL NOT NULL,
                   created_by TEXT)"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_visitors_name ON vm_visitors (name_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_visitors_mobile ON vm_visitors (mobile_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_visitors_email ON vm_visitors (email)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS vm_visits (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   visit_code TEXT UNIQUE,
                   visitor_id INTEGER NOT NULL REFERENCES vm_visitors(id) ON DELETE CASCADE,
                   visitor_name TEXT NOT NULL,      -- as registered for THIS visit
                   visitor_company TEXT,
                   visit_date TEXT NOT NULL,        -- local YYYY-MM-DD
                   purpose TEXT NOT NULL,
                   host_employee_id TEXT,
                   host_name TEXT NOT NULL,
                   host_department TEXT,
                   gate_camera_id INTEGER,
                   notes TEXT,
                   snapshot_consent INTEGER NOT NULL DEFAULT 0,
                   snapshot_status TEXT NOT NULL DEFAULT 'none',  -- none | declined | capturing | captured | no_clear_face | unavailable | expired
                   status TEXT NOT NULL DEFAULT 'pending',         -- pending | in | out | cancelled
                   registered_at REAL NOT NULL,
                   entry_at REAL, entry_source TEXT, entry_camera_id INTEGER,
                   host_notified_at REAL,
                   meeting_start_at REAL,
                   exit_at REAL, exit_source TEXT, exit_camera_id INTEGER,
                   created_by TEXT,
                   updated_at REAL NOT NULL)"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_visits_visitor ON vm_visits (visitor_id, visit_date)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_visits_date ON vm_visits (visit_date, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_visits_host ON vm_visits (host_employee_id)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS vm_visit_events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   visit_id INTEGER NOT NULL REFERENCES vm_visits(id) ON DELETE CASCADE,
                   kind TEXT NOT NULL,              -- registered | entry | exit | host_notified | meeting_start | cancelled
                   ts REAL,                         -- NULL for a correction that clears a time
                   source TEXT NOT NULL,            -- operator | camera | correction | system
                   camera_id INTEGER,
                   camera_name TEXT,
                   source_ref TEXT,                 -- the camera detection linked (ff:/fx:/va:/vx:...)
                   actor TEXT,
                   note TEXT,
                   voided INTEGER NOT NULL DEFAULT 0,
                   created_at REAL NOT NULL)"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_events_visit ON vm_visit_events (visit_id, kind)")
        # One camera detection belongs to at most one visit.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_vm_events_ref ON vm_visit_events (source_ref) "
                     "WHERE source_ref IS NOT NULL AND voided = 0")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS vm_visit_media (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   visit_id INTEGER NOT NULL REFERENCES vm_visits(id) ON DELETE CASCADE,
                   kind TEXT NOT NULL,              -- snapshot | clip
                   moment TEXT,                     -- entry | exit | live
                   path TEXT NOT NULL,              -- relative to DATA_DIR
                   captured_at REAL NOT NULL,
                   camera_id INTEGER,
                   camera_name TEXT,
                   source TEXT NOT NULL,            -- detection_clip | live_capture
                   quality REAL, face_conf REAL, face_px INTEGER, sharpness REAL,
                   width INTEGER, height INTEGER,
                   expires_at REAL NOT NULL,
                   created_at REAL NOT NULL)"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_media_visit ON vm_visit_media (visit_id, kind)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_vm_media_exp ON vm_visit_media (expires_at)")
        conn.execute("CREATE TABLE IF NOT EXISTS vm_settings (key TEXT PRIMARY KEY, value TEXT)")


# --------------------------------------------------------------------------- helpers

def _now() -> float:
    return time.time()


def _today(now: float | None = None) -> str:
    return datetime.datetime.fromtimestamp(now if now is not None else _now()).strftime("%Y-%m-%d")


def _day_bounds(day: str) -> tuple[float, float]:
    d = datetime.datetime.strptime(day, "%Y-%m-%d")
    return d.timestamp(), (d + datetime.timedelta(days=1)).timestamp()


def _check_day(day: str | None, field: str = "date") -> str | None:
    if day in (None, ""):
        return None
    try:
        datetime.datetime.strptime(day, "%Y-%m-%d")
    except (TypeError, ValueError):
        raise RegistryError(f"{field} must be a date (YYYY-MM-DD).")
    return day


def _clean(s, limit: int = 200) -> str | None:
    if s is None:
        return None
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s[:limit] or None


def _name_key(name: str) -> str:
    return re.sub(r"\s+", " ", name or "").strip().lower()


def _mobile_key(mobile: str | None) -> str | None:
    digits = re.sub(r"\D", "", mobile or "")
    return digits[-10:] if len(digits) >= 7 else None


def _check_mobile(mobile):
    mobile = _clean(mobile, 30)
    if mobile and not re.fullmatch(r"\+?[\d\s\-()]{7,20}", mobile):
        raise RegistryError("Mobile number should contain digits only (7-20 characters, + allowed).")
    return mobile


def _check_email(email):
    email = _clean(email, 120)
    if email:
        email = email.lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise RegistryError("E-mail address is not valid.")
    return email


def _actor(principal) -> str | None:
    if not principal:
        return None
    return principal.get("email") or principal.get("license_id") and f"license:{principal['license_id']}"


def _camera_name(camera_id) -> str | None:
    if camera_id is None:
        return None
    cam = camera_db.get_camera(int(camera_id))
    return (cam or {}).get("name") or f"Camera {camera_id}"


# --------------------------------------------------------------------------- settings

def get_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    with _conn() as conn:
        for r in conn.execute("SELECT key, value FROM vm_settings"):
            if r["key"] in s:
                s[r["key"]] = _decode_setting(r["key"], r["value"])
    s["notice"] = s["notice_text"].replace("{days}", str(s["media_retention_days"]))
    return s


def _decode_setting(key, value):
    if value is None:
        return None
    if key in ("media_retention_days", "long_stay_minutes", "waiting_alert_minutes", "default_gate_camera_id"):
        return int(value)
    return value


def set_settings(changes: dict, principal=None) -> dict:
    allowed = {}
    if "media_retention_days" in changes:
        d = int(changes["media_retention_days"])
        if not 1 <= d <= 365:
            raise RegistryError("Photo/clip retention must be between 1 and 365 days.")
        allowed["media_retention_days"] = d
    if "long_stay_minutes" in changes:
        m = int(changes["long_stay_minutes"])
        if not 10 <= m <= 24 * 60:
            raise RegistryError("Long-stay threshold must be between 10 minutes and 24 hours.")
        allowed["long_stay_minutes"] = m
    if "waiting_alert_minutes" in changes:
        m = int(changes["waiting_alert_minutes"])
        if not 1 <= m <= 240:
            raise RegistryError("Waiting threshold must be between 1 and 240 minutes.")
        allowed["waiting_alert_minutes"] = m
    if "default_gate_camera_id" in changes:
        cid = changes["default_gate_camera_id"]
        if cid not in (None, ""):
            if camera_db.get_camera(int(cid)) is None:
                raise RegistryError("Camera not found.")
            cid = int(cid)
        allowed["default_gate_camera_id"] = cid or None
    if "notice_text" in changes:
        t = _clean(changes["notice_text"], 1000)
        if not t:
            raise RegistryError("The visitor notice cannot be empty.")
        allowed["notice_text"] = t
    with _conn() as conn:
        for k, v in allowed.items():
            conn.execute("INSERT OR REPLACE INTO vm_settings (key, value) VALUES (?, ?)", (k, None if v is None else str(v)))
        if "media_retention_days" in allowed:
            # Existing photos/clips follow the new retention too.
            conn.execute("UPDATE vm_visit_media SET expires_at = captured_at + ?", (allowed["media_retention_days"] * 86400,))
    audit.record("visitor_records.settings", principal, None, **allowed)
    if "media_retention_days" in allowed:
        purge_expired_media()
    return get_settings()


# --------------------------------------------------------------------------- visitors

def _visitor_public(r) -> dict:
    return {"id": r["id"], "visitor_code": r["visitor_code"], "name": r["name"], "company": r["company"],
            "mobile": r["mobile"], "email": r["email"], "notes": r["notes"], "created_at": r["created_at"]}


def find_matches(name: str | None = None, mobile: str | None = None, email: str | None = None,
                 company: str | None = None, limit: int = 5) -> list[dict]:
    """Registered visitors who may be the same person, strongest first.
    strength 'strong' = same mobile or e-mail; 'name' = same or very similar
    name (and company, when both are given). The operator decides."""
    mk, em, nk = _mobile_key(mobile), (email or "").strip().lower() or None, _name_key(name or "")
    comp = _name_key(company or "")
    found: dict[int, dict] = {}
    with _conn() as conn:
        if mk:
            for r in conn.execute("SELECT * FROM vm_visitors WHERE mobile_key = ?", (mk,)):
                found[r["id"]] = {**_visitor_public(r), "reason": "Same mobile number", "strength": "strong"}
        if em:
            for r in conn.execute("SELECT * FROM vm_visitors WHERE email = ?", (em,)):
                found.setdefault(r["id"], {**_visitor_public(r), "reason": "Same e-mail", "strength": "strong"})
        if nk and len(nk) >= 3:
            first = nk.split(" ")[0]
            rows = conn.execute("SELECT * FROM vm_visitors WHERE name_key LIKE ? LIMIT 300", (f"%{first[:3]}%",)).fetchall()
            for r in rows:
                if r["id"] in found:
                    continue
                ratio = difflib.SequenceMatcher(None, nk, r["name_key"]).ratio()
                same_company = bool(comp and r["company"] and _name_key(r["company"]) == comp)
                if ratio >= 0.97 or (ratio >= 0.85 and (same_company or not comp)):
                    found[r["id"]] = {**_visitor_public(r), "strength": "name",
                                      "reason": "Same name" + (" and company" if same_company else "") if ratio >= 0.97 else "Similar name"}
    out = sorted(found.values(), key=lambda m: (m["strength"] != "strong", m["name"]))[:limit]
    for m in out:
        m.update(_visit_counts(m["id"]))
    return out


def _visit_counts(visitor_id: int, now: float | None = None) -> dict:
    month = _today(now)[:7]
    with _conn() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS total, MIN(visit_date) AS first, MAX(visit_date) AS last, "
            "SUM(CASE WHEN substr(visit_date, 1, 7) = ? THEN 1 ELSE 0 END) AS month "
            "FROM vm_visits WHERE visitor_id = ? AND status != 'cancelled'", (month, visitor_id)).fetchone()
    return {"total_visits": r["total"] or 0, "first_visit": r["first"], "last_visit": r["last"], "visits_this_month": r["month"] or 0}


def _create_visitor(conn, name, company, mobile, email, notes, actor, now) -> int:
    cur = conn.execute(
        "INSERT INTO vm_visitors (name, name_key, company, mobile, mobile_key, email, notes, created_at, updated_at, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, _name_key(name), company, mobile, _mobile_key(mobile), email, notes, now, now, actor))
    vid = cur.lastrowid
    conn.execute("UPDATE vm_visitors SET visitor_code = ? WHERE id = ?", (f"VIS-{vid:06d}", vid))
    return vid


def update_visitor(visitor_id: int, fields: dict, principal=None) -> dict:
    """Edits the PROFILE only; past visits keep the name/company they were registered with."""
    sets, params = [], []
    if "name" in fields:
        name = _clean(fields["name"], 120)
        if not name:
            raise RegistryError("Visitor name is required.")
        sets += ["name = ?", "name_key = ?"]
        params += [name, _name_key(name)]
    if "company" in fields:
        sets.append("company = ?")
        params.append(_clean(fields["company"], 120))
    if "mobile" in fields:
        m = _check_mobile(fields["mobile"])
        sets += ["mobile = ?", "mobile_key = ?"]
        params += [m, _mobile_key(m)]
    if "email" in fields:
        sets.append("email = ?")
        params.append(_check_email(fields["email"]))
    if "notes" in fields:
        sets.append("notes = ?")
        params.append(_clean(fields["notes"], 1000))
    if not sets:
        return get_profile(visitor_id)
    with _conn() as conn:
        cur = conn.execute(f"UPDATE vm_visitors SET {', '.join(sets)}, updated_at = ? WHERE id = ?", (*params, _now(), visitor_id))
        if cur.rowcount == 0:
            raise KeyError(visitor_id)
    audit.record("visitor_records.profile_updated", principal, f"visitor:{visitor_id}", fields=sorted(k for k in fields))
    return get_profile(visitor_id)


def delete_visitor(visitor_id: int, principal=None) -> bool:
    """Erases a visitor completely: profile, every visit and event, and their
    photo/clip files (a data-erasure request)."""
    with _conn() as conn:
        row = conn.execute("SELECT visitor_code FROM vm_visitors WHERE id = ?", (visitor_id,)).fetchone()
        if row is None:
            return False
        paths = [r["path"] for r in conn.execute(
            "SELECT m.path FROM vm_visit_media m JOIN vm_visits v ON v.id = m.visit_id WHERE v.visitor_id = ?", (visitor_id,))]
        visit_ids = [r["id"] for r in conn.execute("SELECT id FROM vm_visits WHERE visitor_id = ?", (visitor_id,))]
        conn.execute("DELETE FROM vm_visitors WHERE id = ?", (visitor_id,))
    for p in paths:
        _remove_file(p)
    for vid in visit_ids:
        shutil.rmtree(MEDIA_DIR / str(vid), ignore_errors=True)
    audit.record("visitor_records.visitor_erased", principal, row["visitor_code"], visits=len(visit_ids), files=len(paths))
    return True


def search_visitors(q: str | None = None, day: str | None = None, limit: int = 100) -> list[dict]:
    day = _check_day(day)
    sql, params = "SELECT * FROM vm_visitors p WHERE 1 = 1", []
    if q:
        like = f"%{q.strip().lower()}%"
        digits = re.sub(r"\D", "", q)
        sql += (" AND (p.name_key LIKE ? OR lower(p.visitor_code) LIKE ? OR lower(COALESCE(p.company, '')) LIKE ? "
                "OR COALESCE(p.mobile_key, '') LIKE ? OR COALESCE(p.email, '') LIKE ?)")
        params += [like, like, like, f"%{digits}%" if digits else "\x00", like]
    if day:
        sql += " AND EXISTS (SELECT 1 FROM vm_visits v WHERE v.visitor_id = p.id AND v.visit_date = ? AND v.status != 'cancelled')"
        params.append(day)
    sql += " ORDER BY p.updated_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [{**_visitor_public(r), **_visit_counts(r["id"])} for r in rows]


# --------------------------------------------------------------------------- visits

def register_visit(data: dict, principal=None, now: float | None = None) -> dict:
    """Creates the visit (and the visitor profile when they are new).
    data: name, purpose, host_employee_id | host_name, visit_date, and optional
    company, mobile, email, notes, gate_camera_id, snapshot_consent,
    visitor_id (an existing profile, operator-confirmed) or create_new (the
    operator confirmed a likely match is a different person)."""
    now = now if now is not None else _now()
    actor = _actor(principal)
    purpose = _clean(data.get("purpose"), 200)
    visit_date = _check_day(data.get("visit_date"), "Visit date") or _today(now)
    if not purpose:
        raise RegistryError("Purpose of visit is required.")
    host_id = _clean(data.get("host_employee_id"), 60)
    host_name, host_dept = _clean(data.get("host_name"), 120), None
    if host_id:
        emp = employee_directory.get_employee(host_id)
        if emp is None:
            raise RegistryError("The person to meet is not in the employee list.")
        host_name, host_dept = emp["name"], emp.get("company") or None
    if not host_name:
        raise RegistryError("Person to meet is required.")
    gate = data.get("gate_camera_id")
    if gate in (None, ""):
        gate = get_settings()["default_gate_camera_id"]
    elif camera_db.get_camera(int(gate)) is None:
        raise RegistryError("Gate camera not found.")
    consent = bool(data.get("snapshot_consent"))
    notes = _clean(data.get("notes"), 1000)

    visitor_id = data.get("visitor_id")
    with _conn() as conn:
        if visitor_id:
            prof = conn.execute("SELECT * FROM vm_visitors WHERE id = ?", (int(visitor_id),)).fetchone()
            if prof is None:
                raise RegistryError("Visitor profile not found.")
            visitor_id = prof["id"]
            # New details given at this visit fill the profile in (never blank it).
            fills = {}
            for k, check in (("company", lambda v: _clean(v, 120)), ("mobile", _check_mobile), ("email", _check_email)):
                val = check(data.get(k))
                if val and val != prof[k]:
                    fills[k] = val
            if fills:
                if "mobile" in fills:
                    fills["mobile_key"] = _mobile_key(fills["mobile"])
                conn.execute(f"UPDATE vm_visitors SET {', '.join(f'{k} = ?' for k in fills)}, updated_at = ? WHERE id = ?",
                             (*fills.values(), now, visitor_id))
            name, company = prof["name"], fills.get("company") or prof["company"]
        else:
            name = _clean(data.get("name"), 120)
            if not name:
                raise RegistryError("Visitor name is required.")
            company = _clean(data.get("company"), 120)
            mobile, email = _check_mobile(data.get("mobile")), _check_email(data.get("email"))
            if not data.get("create_new"):
                matches = find_matches(name, mobile, email, company)
                if matches:
                    raise DuplicateVisitor(matches)
            visitor_id = _create_visitor(conn, name, company, mobile, email, None, actor, now)
        cur = conn.execute(
            "INSERT INTO vm_visits (visitor_id, visitor_name, visitor_company, visit_date, purpose, host_employee_id, host_name, "
            "host_department, gate_camera_id, notes, snapshot_consent, snapshot_status, status, registered_at, created_by, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (visitor_id, name, company, visit_date, purpose, host_id, host_name, host_dept, gate, notes,
             int(consent), "none" if consent else "declined", now, actor, now))
        visit_id = cur.lastrowid
        code = f"VST-{visit_date.replace('-', '')}-{visit_id:05d}"
        conn.execute("UPDATE vm_visits SET visit_code = ? WHERE id = ?", (code, visit_id))
        conn.execute("INSERT INTO vm_visit_events (visit_id, kind, ts, source, actor, note, created_at) VALUES (?, 'registered', ?, 'operator', ?, ?, ?)",
                     (visit_id, now, actor, "snapshot consent given" if consent else "snapshot declined", now))
    audit.record("visitor_records.visit_registered", principal, code, visitor_id=visitor_id, consent=consent)
    return get_visit(visit_id, now=now)


_VISIT_SQL = (
    "SELECT v.*, p.visitor_code, p.name AS profile_name, "
    "(SELECT COUNT(*) FROM vm_visits q WHERE q.visitor_id = v.visitor_id AND q.status != 'cancelled' AND q.id != v.id "
    "  AND (q.visit_date < v.visit_date OR (q.visit_date = v.visit_date AND q.id < v.id))) AS prior_visits, "
    "(SELECT m.id FROM vm_visit_media m WHERE m.visit_id = v.id AND m.kind = 'snapshot' ORDER BY m.quality DESC LIMIT 1) AS snapshot_id, "
    "(SELECT COUNT(*) FROM vm_visit_media m WHERE m.visit_id = v.id AND m.kind = 'clip') AS clips "
    "FROM vm_visits v JOIN vm_visitors p ON p.id = v.visitor_id"
)


def _visit_public(r, now: float) -> dict:
    entry, exit_, meet = r["entry_at"], r["exit_at"], r["meeting_start_at"]
    duration = exit_ - entry if entry is not None and exit_ is not None else None
    elapsed = now - entry if entry is not None and exit_ is None and r["status"] == IN else None
    return {
        "id": r["id"], "visit_code": r["visit_code"], "visitor_id": r["visitor_id"], "visitor_code": r["visitor_code"],
        "visitor_name": r["visitor_name"], "visitor_company": r["visitor_company"], "visit_date": r["visit_date"],
        "purpose": r["purpose"], "host_employee_id": r["host_employee_id"], "host_name": r["host_name"],
        "host_department": r["host_department"], "gate_camera_id": r["gate_camera_id"],
        "gate_camera_name": _camera_name(r["gate_camera_id"]), "notes": r["notes"],
        "snapshot_consent": bool(r["snapshot_consent"]), "snapshot_status": r["snapshot_status"],
        "snapshot_id": r["snapshot_id"], "clips": r["clips"], "snapshot_message": _last_capture.get(r["id"]),
        "status": r["status"], "registered_at": r["registered_at"],
        "entry_at": entry, "entry_source": r["entry_source"], "entry_camera_id": r["entry_camera_id"],
        "host_notified_at": r["host_notified_at"], "meeting_start_at": meet,
        "exit_at": exit_, "exit_source": r["exit_source"], "exit_camera_id": r["exit_camera_id"],
        "waiting_seconds": meet - entry if entry is not None and meet is not None else None,
        "duration_seconds": duration, "elapsed_seconds": elapsed,
        "first_visit": r["prior_visits"] == 0, "prior_visits": r["prior_visits"],
    }


def get_visit(visit_id: int, now: float | None = None, with_detail: bool = False) -> dict:
    now = now if now is not None else _now()
    with _conn() as conn:
        r = conn.execute(_VISIT_SQL + " WHERE v.id = ?", (visit_id,)).fetchone()
        if r is None:
            raise KeyError(visit_id)
        out = _visit_public(r, now)
        if with_detail:
            out["events"] = [dict(e) for e in conn.execute(
                "SELECT id, kind, ts, source, camera_id, camera_name, source_ref, actor, note, voided, created_at "
                "FROM vm_visit_events WHERE visit_id = ? ORDER BY created_at, id", (visit_id,))]
            out["media"] = _media_rows(conn, "m.visit_id = ?", (visit_id,))
    return out


def list_visits(date_from=None, date_to=None, status=None, host=None, department=None, visitor_type=None,
                q=None, visitor_id=None, limit: int = 500, now: float | None = None) -> list[dict]:
    now = now if now is not None else _now()
    date_from, date_to = _check_day(date_from, "From date"), _check_day(date_to, "To date")
    sql, params = _VISIT_SQL + " WHERE 1 = 1", []
    if date_from:
        sql += " AND v.visit_date >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND v.visit_date <= ?"
        params.append(date_to)
    if status:
        if status not in (PENDING, IN, OUT, CANCELLED):
            raise RegistryError("Unknown status.")
        sql += " AND v.status = ?"
        params.append(status)
    else:
        sql += " AND v.status != 'cancelled'"
    if host:
        sql += " AND v.host_employee_id = ?"
        params.append(host)
    if department:
        sql += " AND v.host_department = ?"
        params.append(department)
    if visitor_id:
        sql += " AND v.visitor_id = ?"
        params.append(int(visitor_id))
    if q:
        like = f"%{q.strip().lower()}%"
        sql += (" AND (lower(v.visitor_name) LIKE ? OR lower(p.visitor_code) LIKE ? OR lower(v.visit_code) LIKE ? "
                "OR lower(COALESCE(v.visitor_company, '')) LIKE ?)")
        params += [like, like, like, like]
    sql += " ORDER BY v.visit_date DESC, COALESCE(v.entry_at, v.registered_at) DESC, v.id DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = [_visit_public(r, now) for r in conn.execute(sql, params)]
    if visitor_type == "first":
        rows = [r for r in rows if r["first_visit"]]
    elif visitor_type == "repeat":
        rows = [r for r in rows if not r["first_visit"]]
    if rows:
        month_counts = _month_counts({r["visitor_id"] for r in rows}, now)
        for r in rows:
            r["visits_this_month"] = month_counts.get(r["visitor_id"], 0)
    return rows


def _month_counts(visitor_ids, now) -> dict[int, int]:
    ids = list(visitor_ids)
    if not ids:
        return {}
    month = _today(now)[:7]
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT visitor_id, COUNT(*) AS n FROM vm_visits WHERE status != 'cancelled' AND substr(visit_date, 1, 7) = ? "
            f"AND visitor_id IN ({','.join('?' * len(ids))}) GROUP BY visitor_id", (month, *ids)).fetchall()
    return {r["visitor_id"]: r["n"] for r in rows}


def get_profile(visitor_id: int, now: float | None = None) -> dict:
    now = now if now is not None else _now()
    with _conn() as conn:
        r = conn.execute("SELECT * FROM vm_visitors WHERE id = ?", (visitor_id,)).fetchone()
        if r is None:
            raise KeyError(visitor_id)
        media = _media_rows(conn, "v.visitor_id = ?", (visitor_id,))
    return {**_visitor_public(r), **_visit_counts(visitor_id, now),
            "visits": list_visits(visitor_id=visitor_id, limit=1000, now=now), "media": media}


# ---- times: record, correct, recompute -----------------------------------------

def _current_events(conn, visit_id) -> dict[str, sqlite3.Row]:
    out = {}
    for e in conn.execute("SELECT * FROM vm_visit_events WHERE visit_id = ? AND voided = 0 AND kind IN (?, ?, ?, ?, 'cancelled') "
                          "ORDER BY created_at, id", (visit_id, *TIME_KINDS)):
        out[e["kind"]] = e   # the latest active event of each kind wins
    return out


def _validate_times(times: dict, now: float) -> None:
    for k, v in times.items():
        if v is not None and v > now + FUTURE_SKEW_SECONDS:
            raise RegistryError(f"{k.replace('_', ' ').capitalize()} time cannot be in the future.")
    entry, exit_, meet = times.get("entry"), times.get("exit"), times.get("meeting_start")
    if exit_ is not None and entry is None:
        raise RegistryError("Record the entry before the exit.")
    if exit_ is not None and exit_ < entry:
        raise RegistryError("Exit time cannot be before the entry time.")
    if meet is not None:
        if entry is None:
            raise RegistryError("Record the visitor's entry before the meeting start.")
        if meet < entry:
            raise RegistryError("Meeting start cannot be before the visitor arrived.")
        if exit_ is not None and meet > exit_:
            raise RegistryError("Meeting start cannot be after the visitor left.")


def _recompute(conn, visit_id: int) -> None:
    ev = _current_events(conn, visit_id)
    t = {k: (ev[k]["ts"] if k in ev else None) for k in TIME_KINDS}
    if "cancelled" in ev:
        status = CANCELLED
    elif t["exit"] is not None:
        status = OUT
    elif t["entry"] is not None:
        status = IN
    else:
        status = PENDING
    src = lambda k: (ev[k]["source"] if k in ev and ev[k]["ts"] is not None else None)   # noqa: E731
    cam = lambda k: (ev[k]["camera_id"] if k in ev and ev[k]["ts"] is not None else None)  # noqa: E731
    conn.execute(
        "UPDATE vm_visits SET status = ?, entry_at = ?, entry_source = ?, entry_camera_id = ?, exit_at = ?, exit_source = ?, "
        "exit_camera_id = ?, host_notified_at = ?, meeting_start_at = ?, updated_at = ? WHERE id = ?",
        (status, t["entry"], src("entry"), cam("entry"), t["exit"], src("exit"), cam("exit"),
         t["host_notified"], t["meeting_start"], _now(), visit_id))


def record_time(visit_id: int, kind: str, ts: float | None = None, principal=None, note: str | None = None,
                source: str = "operator", camera_id: int | None = None, source_ref: str | None = None,
                now: float | None = None) -> dict:
    """Records a time that is not set yet (check in, check out, host notified,
    meeting started). To change a time already recorded use correct_time()."""
    now = now if now is not None else _now()
    if kind not in TIME_KINDS:
        raise RegistryError("Unknown time.")
    ts = float(ts) if ts is not None else now
    with _conn() as conn:
        visit = conn.execute("SELECT status FROM vm_visits WHERE id = ?", (visit_id,)).fetchone()
        if visit is None:
            raise KeyError(visit_id)
        if visit["status"] == CANCELLED:
            raise RegistryError("This visit was cancelled.")
        ev = _current_events(conn, visit_id)
        if kind in ev and ev[kind]["ts"] is not None:
            raise RegistryError(f"{_LABEL[kind]} is already recorded. Use 'Correct' to change it.")
        times = {k: (ev[k]["ts"] if k in ev else None) for k in TIME_KINDS}
        times[kind] = ts
        _validate_times(times, now)
        try:
            conn.execute(
                "INSERT INTO vm_visit_events (visit_id, kind, ts, source, camera_id, camera_name, source_ref, actor, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (visit_id, kind, ts, source, camera_id, _camera_name(camera_id), source_ref, _actor(principal), _clean(note, 300), now))
        except sqlite3.IntegrityError:
            raise RegistryError("That camera detection is already linked to another visit.")
        _recompute(conn, visit_id)
    audit.record(f"visitor_records.{kind}", principal, f"visit:{visit_id}", source=source, ts=ts, ref=source_ref)
    return get_visit(visit_id, now=now)


_LABEL = {"entry": "Entry", "exit": "Exit", "host_notified": "Host notification", "meeting_start": "Meeting start"}


def correct_time(visit_id: int, kind: str, ts: float | None, note: str, principal=None, now: float | None = None) -> dict:
    """Manual correction of a missed or wrong time (ts None clears it). The old
    event is kept, marked voided; a reason is required."""
    now = now if now is not None else _now()
    if kind not in TIME_KINDS:
        raise RegistryError("Unknown time.")
    note = _clean(note, 300)
    if not note:
        raise RegistryError("Give a reason for the correction.")
    with _conn() as conn:
        visit = conn.execute("SELECT status FROM vm_visits WHERE id = ?", (visit_id,)).fetchone()
        if visit is None:
            raise KeyError(visit_id)
        if visit["status"] == CANCELLED:
            raise RegistryError("This visit was cancelled.")
        ev = _current_events(conn, visit_id)
        times = {k: (ev[k]["ts"] if k in ev else None) for k in TIME_KINDS}
        times[kind] = float(ts) if ts is not None else None
        _validate_times(times, now)
        if kind in ev:
            conn.execute("UPDATE vm_visit_events SET voided = 1 WHERE visit_id = ? AND kind = ? AND voided = 0", (visit_id, kind))
        conn.execute(
            "INSERT INTO vm_visit_events (visit_id, kind, ts, source, actor, note, created_at) VALUES (?, ?, ?, 'correction', ?, ?, ?)",
            (visit_id, kind, times[kind], _actor(principal), note, now))
        _recompute(conn, visit_id)
    audit.record("visitor_records.corrected", principal, f"visit:{visit_id}", kind=kind, ts=times[kind], note=note)
    return get_visit(visit_id, now=now)


def cancel_visit(visit_id: int, principal=None, note: str | None = None) -> dict:
    with _conn() as conn:
        v = conn.execute("SELECT status FROM vm_visits WHERE id = ?", (visit_id,)).fetchone()
        if v is None:
            raise KeyError(visit_id)
        if v["status"] != PENDING:
            raise RegistryError("Only a visit that has not checked in yet can be cancelled.")
        conn.execute("INSERT INTO vm_visit_events (visit_id, kind, ts, source, actor, note, created_at) VALUES (?, 'cancelled', ?, 'operator', ?, ?, ?)",
                     (visit_id, _now(), _actor(principal), _clean(note, 300), _now()))
        _recompute(conn, visit_id)
    audit.record("visitor_records.cancelled", principal, f"visit:{visit_id}")
    return get_visit(visit_id)


def delete_visit(visit_id: int, principal=None) -> bool:
    with _conn() as conn:
        r = conn.execute("SELECT visit_code FROM vm_visits WHERE id = ?", (visit_id,)).fetchone()
        if r is None:
            return False
        paths = [m["path"] for m in conn.execute("SELECT path FROM vm_visit_media WHERE visit_id = ?", (visit_id,))]
        conn.execute("DELETE FROM vm_visits WHERE id = ?", (visit_id,))
    for p in paths:
        _remove_file(p)
    shutil.rmtree(MEDIA_DIR / str(visit_id), ignore_errors=True)
    audit.record("visitor_records.visit_deleted", principal, r["visit_code"])
    return True


# --------------------------------------------------------------------------- camera detections

def camera_capabilities() -> list[dict]:
    """What each configured camera can tell this module, so the UI can say
    plainly what is and isn't available (never simulated)."""
    from app import visitors
    from app.reid import reid_db

    gates = {}
    try:
        for c in reid_db.list_camera_configs():
            gates[c["camera_id"]] = c
    except Exception:
        log.exception("visitor_records: could not read footfall camera settings")
    zones = {z["camera_id"]: z for z in visitors.list_zones()}
    out = []
    for cam in camera_db.list_cameras():
        g = gates.get(cam["id"]) or {}
        footfall_on = bool(g.get("enabled"))
        line = bool(g.get("entry_line"))
        zone = zones.get(cam["id"])
        out.append({
            "camera_id": cam["id"], "name": cam["name"],
            "entry_detection": footfall_on or bool(zone and zone["enabled"]),
            "exit_detection": footfall_on and line,
            "footfall": footfall_on, "entry_line": line, "visitor_area": bool(zone and zone["enabled"]),
        })
    return out


def detections(day: str, kind: str | None = None) -> list[dict]:
    """Camera events of one day that can be linked to a visit, newest first.
    Each says whether it is already linked (and to which visit). Employees the
    footfall camera recognised are left out; they are not visitors.
      ff:<clip id>   footfall: a person counted entering at a gate camera
      fx:<event id>  footfall: a person crossing a gate's entry line outwards
      va:<visit>     visitor seating area: an unknown person arrived
      vx:<visit>     visitor seating area: that person left"""
    from app import visitors
    from app.reid import reid_db

    day = _check_day(day) or _today()
    t0, t1 = _day_bounds(day)
    items: list[dict] = []
    try:
        with reid_db.get_connection() as conn:
            for cid, cam, ts, status in conn.execute(
                    "SELECT id, camera_id, ts, clip_status FROM footfall_clips WHERE day = ? AND employee_id IS NULL", (day,)):
                items.append({"ref": f"ff:{cid}", "kind": "entry", "ts": ts, "camera_id": cam, "source": "Gate entry (footfall)",
                              "clip": status == "ready"})
            for eid, cam, ts in conn.execute("SELECT id, camera_id, ts FROM reid_events WHERE event_type = ? AND ts >= ? AND ts < ?",
                                             (reid_db.EVENT_EXIT, t0, t1)):
                items.append({"ref": f"fx:{eid}", "kind": "exit", "ts": ts, "camera_id": cam, "source": "Gate exit (footfall)", "clip": False})
    except sqlite3.Error:
        log.exception("visitor_records: could not read footfall detections")
    for v in visitors.list_visits(limit=2000, since=t0, until=t1):
        items.append({"ref": f"va:{v['visit']}", "kind": "entry", "ts": v["arrived_at"], "camera_id": v["camera_id"],
                      "source": "Visitor area arrival", "clip": bool(v["latest_event_id"])})
        if v["status"] == "left" and v["left_at"]:
            items.append({"ref": f"vx:{v['visit']}", "kind": "exit", "ts": v["left_at"], "camera_id": v["camera_id"],
                          "source": "Left visitor area", "clip": False})
    if kind:
        items = [i for i in items if i["kind"] == kind]
    refs = [i["ref"] for i in items]
    linked = {}
    if refs:
        with _conn() as conn:
            for r in conn.execute(
                    f"SELECT e.source_ref, v.id, v.visit_code, v.visitor_name FROM vm_visit_events e JOIN vm_visits v ON v.id = e.visit_id "
                    f"WHERE e.voided = 0 AND e.source_ref IN ({','.join('?' * len(refs))})", refs):
                linked[r["source_ref"]] = {"visit_id": r["id"], "visit_code": r["visit_code"], "visitor_name": r["visitor_name"]}
    for i in items:
        i["camera_name"] = _camera_name(i["camera_id"])
        i["linked"] = linked.get(i["ref"])
    items.sort(key=lambda i: i["ts"], reverse=True)
    return items


def _detection(ref: str) -> dict | None:
    """One detection by ref, with its clip file (if any) - looked up afresh."""
    from app import footfall, visitors
    from app.reid import reid_db

    kind, _, key = (ref or "").partition(":")
    if kind == "ff" and key.isdigit():
        with reid_db.get_connection() as conn:
            r = conn.execute("SELECT camera_id, ts, employee_id FROM footfall_clips WHERE id = ?", (int(key),)).fetchone()
        if r is None or r[2]:
            return None
        path, _why = footfall.FootfallService.clip_file(int(key))
        return {"ref": ref, "kind": "entry", "camera_id": r[0], "ts": r[1], "clip_path": path}
    if kind == "fx" and key.isdigit():
        with reid_db.get_connection() as conn:
            r = conn.execute("SELECT camera_id, ts FROM reid_events WHERE id = ? AND event_type = ?", (int(key), reid_db.EVENT_EXIT)).fetchone()
        return None if r is None else {"ref": ref, "kind": "exit", "camera_id": r[0], "ts": r[1], "clip_path": None}
    if kind in ("va", "vx"):
        for v in visitors.list_visits(limit=5000):
            if v["visit"] == key:
                if kind == "vx":
                    if v["status"] != "left" or not v["left_at"]:
                        return None
                    return {"ref": ref, "kind": "exit", "camera_id": v["camera_id"], "ts": v["left_at"], "clip_path": None}
                path = visitors.clip_file(v["latest_event_id"])[0] if v["latest_event_id"] else None
                return {"ref": ref, "kind": "entry", "camera_id": v["camera_id"], "ts": v["arrived_at"], "clip_path": path}
    return None


def candidates(visit_id: int, now: float | None = None) -> dict:
    """Unlinked camera detections that could be this visit's next time: entry
    detections for a visit not checked in yet, exit ones for a visit inside.
    Ordered by how close they are to when the visit was expected, the visit's
    gate camera first. Suggestions only; the operator confirms."""
    v = get_visit(visit_id, now=now)
    if v["status"] == PENDING:
        want, ref_ts = "entry", v["registered_at"]
    elif v["status"] == IN:
        want, ref_ts = "exit", v["entry_at"]
    else:
        return {"kind": None, "detections": []}
    items = [d for d in detections(v["visit_date"], want) if not d["linked"]]
    if want == "exit":
        items = [d for d in items if d["ts"] >= v["entry_at"]]
    else:
        items = [d for d in items if d["ts"] >= v["registered_at"] - 2 * 3600]
    gate = v["gate_camera_id"]
    items.sort(key=lambda d: (gate is not None and d["camera_id"] != gate, abs(d["ts"] - ref_ts)))
    return {"kind": want, "detections": items[:20]}


def link_detection(visit_id: int, ref: str, principal=None, now: float | None = None) -> dict:
    """The operator confirmed that camera detection `ref` is this visitor:
    it becomes the visit's entry (or exit). With consent, the detection's clip
    is kept with the visit and a snapshot is taken from it (in the background)."""
    det = _detection(ref)
    if det is None:
        raise RegistryError("That camera detection no longer exists.")
    visit = record_time(visit_id, det["kind"], det["ts"], principal, note="confirmed by operator", source="camera",
                        camera_id=det["camera_id"], source_ref=ref, now=now)
    if visit["snapshot_consent"] and det.get("clip_path"):
        threading.Thread(target=_attach_clip_and_snapshot, args=(visit_id, det), daemon=True,
                         name=f"visitor-media-{visit_id}").start()
    return visit


# --------------------------------------------------------------------------- media

def _media_rows(conn, where: str, params) -> list[dict]:
    rows = conn.execute(
        f"SELECT m.id, m.visit_id, v.visit_code, v.visit_date, m.kind, m.moment, m.captured_at, m.camera_id, m.camera_name, "
        f"m.source, m.quality, m.face_px, m.expires_at FROM vm_visit_media m JOIN vm_visits v ON v.id = m.visit_id "
        f"WHERE {where} ORDER BY m.captured_at DESC", params).fetchall()
    return [dict(r) for r in rows]


def media_file(media_id: int) -> tuple[Path | None, str | None]:
    with _conn() as conn:
        r = conn.execute("SELECT path, kind, expires_at FROM vm_visit_media WHERE id = ?", (media_id,)).fetchone()
    if r is None or r["expires_at"] <= _now():
        return None, None
    p = (Path(config.DATA_DIR) / r["path"]).resolve()
    try:
        p.relative_to(MEDIA_DIR.resolve())
    except ValueError:
        return None, None
    return (p if p.is_file() else None), ("image/jpeg" if r["kind"] == "snapshot" else "video/mp4")


def delete_media(media_id: int, principal=None) -> bool:
    with _conn() as conn:
        r = conn.execute("SELECT visit_id, path, kind FROM vm_visit_media WHERE id = ?", (media_id,)).fetchone()
        if r is None:
            return False
        conn.execute("DELETE FROM vm_visit_media WHERE id = ?", (media_id,))
        if r["kind"] == "snapshot":
            left = conn.execute("SELECT COUNT(*) FROM vm_visit_media WHERE visit_id = ? AND kind = 'snapshot'", (r["visit_id"],)).fetchone()[0]
            if not left:
                conn.execute("UPDATE vm_visits SET snapshot_status = 'none' WHERE id = ? AND snapshot_status = 'captured'", (r["visit_id"],))
    _remove_file(r["path"])
    audit.record("visitor_records.media_deleted", principal, f"visit:{r['visit_id']}", kind=r["kind"])
    return True


def _remove_file(rel: str | None) -> None:
    if not rel:
        return
    p = (Path(config.DATA_DIR) / rel).resolve()
    try:
        p.relative_to(MEDIA_DIR.resolve())
    except ValueError:
        return
    p.unlink(missing_ok=True)


def purge_expired_media(now: float | None = None) -> int:
    now = now if now is not None else _now()
    with _conn() as conn:
        rows = conn.execute("SELECT id, visit_id, path FROM vm_visit_media WHERE expires_at <= ?", (now,)).fetchall()
        conn.executemany("DELETE FROM vm_visit_media WHERE id = ?", [(r["id"],) for r in rows])
        for vid in {r["visit_id"] for r in rows}:
            if not conn.execute("SELECT 1 FROM vm_visit_media WHERE visit_id = ? AND kind = 'snapshot'", (vid,)).fetchone():
                conn.execute("UPDATE vm_visits SET snapshot_status = 'expired' WHERE id = ? AND snapshot_status = 'captured'", (vid,))
    for r in rows:
        _remove_file(r["path"])
    if rows:
        log.info("visitor_records: removed %d expired photo/clip file(s)", len(rows))
    return len(rows)


def _consenting(visit_id: int) -> bool:
    with _conn() as conn:
        r = conn.execute("SELECT snapshot_consent FROM vm_visits WHERE id = ?", (visit_id,)).fetchone()
    return bool(r and r["snapshot_consent"])


def _add_media(visit_id, kind, moment, rel, captured_at, camera_id, source, q=None) -> int:
    days = get_settings()["media_retention_days"]
    q = q or {}
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO vm_visit_media (visit_id, kind, moment, path, captured_at, camera_id, camera_name, source, quality, face_conf, "
            "face_px, sharpness, width, height, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (visit_id, kind, moment, rel, captured_at, camera_id, _camera_name(camera_id), source, q.get("quality"), q.get("face_conf"),
             q.get("face_px"), q.get("sharpness"), q.get("width"), q.get("height"), captured_at + days * 86400, _now()))
        return cur.lastrowid


def _attach_clip_and_snapshot(visit_id: int, det: dict) -> None:
    try:
        if not _consenting(visit_id):
            return
        src = Path(det["clip_path"])
        dest = MEDIA_DIR / str(visit_id) / f"{det['kind']}_clip_{int(det['ts'])}.mp4"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        _add_media(visit_id, "clip", det["kind"], dest.relative_to(config.DATA_DIR).as_posix(), det["ts"], det["camera_id"], "detection_clip")
        if det["kind"] == "entry":
            _set_snapshot_status(visit_id, "capturing")
            _snapshot_worker(visit_id, _clip_frames(dest), det["ts"], det["camera_id"], "detection_clip", "entry")
    except Exception:
        log.exception("visitor_records: could not attach media to visit %s", visit_id)


def _clip_frames(path: Path, max_frames: int = CLIP_SNAPSHOT_FRAMES) -> list[np.ndarray]:
    """Up to max_frames frames spread evenly over the clip."""
    import av

    with av.open(str(path)) as container:
        frames = [f.to_ndarray(format="bgr24") for f in container.decode(video=0)]
    if len(frames) <= max_frames:
        return frames
    step = len(frames) / max_frames
    return [frames[int(i * step)] for i in range(max_frames)]


# The outcome of the last snapshot attempt per visit (shown in the visit panel).
_last_capture: dict[int, str] = {}


def _snapshot_worker(visit_id, frames, captured_at, camera_id, source, moment) -> None:
    """Runs the (slow) face check off the request thread; the visit shows
    'capturing' meanwhile and its real outcome afterwards."""
    try:
        result = _save_best_snapshot(visit_id, frames, captured_at, camera_id, source, moment)
        _last_capture[visit_id] = (f"Saved the clearest face of the {len(frames)} frames checked." if result["saved"]
                                   else result["reason"])
    except Exception:
        log.exception("visitor_records: snapshot failed for visit %s", visit_id)
        _last_capture[visit_id] = "The snapshot could not be taken (server error)."
        _set_snapshot_status(visit_id, "none", only_if_not_captured=True)
    finally:
        with _conn() as conn:
            has = conn.execute("SELECT 1 FROM vm_visit_media WHERE visit_id = ? AND kind = 'snapshot'", (visit_id,)).fetchone()
            if has:
                conn.execute("UPDATE vm_visits SET snapshot_status = 'captured' WHERE id = ?", (visit_id,))
            else:
                conn.execute("UPDATE vm_visits SET snapshot_status = 'none' WHERE id = ? AND snapshot_status = 'capturing'", (visit_id,))


def _start_snapshot(visit_id, frames, captured_at, camera_id, source, moment) -> None:
    _set_snapshot_status(visit_id, "capturing")
    _last_capture.pop(visit_id, None)
    threading.Thread(target=_snapshot_worker, args=(visit_id, frames, captured_at, camera_id, source, moment),
                     daemon=True, name=f"visitor-snapshot-{visit_id}").start()


_detector_lock = threading.Lock()


def detect_faces(img: np.ndarray) -> list[tuple[float, float, float, float, float]] | None:
    """Face BOXES only (x1, y1, x2, y2, confidence), from the face DETECTOR the
    app already uses. No recognition, no embeddings. None when the detector
    cannot be loaded."""
    try:
        from app.face_pipeline import CameraFacePipeline

        CameraFacePipeline._ensure_yolo_loaded()
        model = CameraFacePipeline._yolo
    except Exception:
        log.exception("visitor_records: face detector unavailable")
        return None
    with _detector_lock:
        res = model.predict(img, verbose=False, conf=0.35)[0]
    if res.boxes is None or len(res.boxes) == 0:
        return []
    boxes = res.boxes.xyxy.cpu().numpy()
    confs = res.boxes.conf.cpu().numpy()
    return [(float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(c)) for b, c in zip(boxes, confs)]


def face_quality(img: np.ndarray, box) -> dict | None:
    """Scores one detected face; None if it is not clear enough to keep
    (too small, low confidence, blurred, too dark or too bright)."""
    x1, y1, x2, y2, conf = box
    side = min(x2 - x1, y2 - y1)
    if conf < MIN_FACE_CONF or side < MIN_FACE_SIDE:
        return None
    h, w = img.shape[:2]
    crop = img[max(0, int(y1)):min(h, int(y2)), max(0, int(x1)):min(w, int(x2))]
    if crop.size == 0:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    bright = float(gray.mean())
    if sharp < MIN_FACE_BLUR or not MIN_BRIGHTNESS <= bright <= MAX_BRIGHTNESS:
        return None
    quality = conf * min(1.0, side / 120.0) * min(1.0, sharp / 250.0)
    return {"quality": round(quality, 4), "face_conf": round(conf, 3), "face_px": int(side), "sharpness": round(sharp, 1)}


def _portrait(img: np.ndarray, box) -> np.ndarray:
    """Head-and-shoulders crop around the face (more than the face, so it reads
    as a visit photo, not a biometric template)."""
    x1, y1, x2, y2 = box[:4]
    fw, fh = x2 - x1, y2 - y1
    h, w = img.shape[:2]
    cx = (x1 + x2) / 2
    left, right = int(max(0, cx - 1.6 * fw)), int(min(w, cx + 1.6 * fw))
    top, bottom = int(max(0, y1 - 0.8 * fh)), int(min(h, y2 + 1.9 * fh))
    return img[top:bottom, left:right]


def best_face(frames: list[np.ndarray]) -> tuple[np.ndarray, dict] | None | str:
    """The single clearest face across the frames: (frame, quality incl. box),
    None if none was clear enough, or 'unavailable' without a detector."""
    best = None
    for img in frames:
        faces = detect_faces(img)
        if faces is None:
            return "unavailable"
        for box in faces:
            q = face_quality(img, box)
            if q and (best is None or q["quality"] > best[1]["quality"]):
                best = (img, {**q, "box": box})
    return best


def _save_best_snapshot(visit_id, frames, captured_at, camera_id, source, moment) -> dict:
    """Keeps ONE snapshot per visit: the new one only if it is clearer than
    the one already saved (so no pile of near-duplicates)."""
    if not _consenting(visit_id):
        return {"saved": False, "reason": "The visitor did not agree to a snapshot."}
    found = best_face(frames) if frames else None
    if found == "unavailable":
        _set_snapshot_status(visit_id, "unavailable", only_if_not_captured=True)
        return {"saved": False, "reason": "The face detector is not available on this server, so no snapshot was taken."}
    if found is None:
        _set_snapshot_status(visit_id, "no_clear_face", only_if_not_captured=True)
        return {"saved": False, "reason": "No clear, front-facing face was visible, so no snapshot was saved."}
    img, q = found
    with _conn() as conn:
        old = conn.execute("SELECT id, quality, path FROM vm_visit_media WHERE visit_id = ? AND kind = 'snapshot'", (visit_id,)).fetchall()
    if old and max(o["quality"] or 0 for o in old) >= q["quality"]:
        return {"saved": False, "reason": "The saved snapshot is already the clearest one."}
    crop = _portrait(img, q["box"])
    ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        return {"saved": False, "reason": "Could not encode the snapshot."}
    dest = MEDIA_DIR / str(visit_id) / f"snapshot_{int(captured_at * 1000)}.jpg"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(buf.tobytes())
    mid = _add_media(visit_id, "snapshot", moment, dest.relative_to(config.DATA_DIR).as_posix(), captured_at, camera_id, source,
                     {**q, "width": crop.shape[1], "height": crop.shape[0]})
    for o in old:  # replaced by a clearer one
        with _conn() as conn:
            conn.execute("DELETE FROM vm_visit_media WHERE id = ?", (o["id"],))
        _remove_file(o["path"])
    _set_snapshot_status(visit_id, "captured")
    return {"saved": True, "media_id": mid, "quality": q["quality"]}


def _set_snapshot_status(visit_id, status, only_if_not_captured=False):
    with _conn() as conn:
        sql = "UPDATE vm_visits SET snapshot_status = ? WHERE id = ?"
        if only_if_not_captured:
            sql += " AND snapshot_status != 'captured'"
        conn.execute(sql, (status, visit_id))


def capture_live(visit_id: int, camera_id: int | None = None, principal=None) -> dict:
    """Takes a few frames from a camera now (e.g. the reception camera while the
    visitor is at the desk); the clearest face, if any, is kept. Only with consent."""
    from app import camera_stream

    v = get_visit(visit_id)
    if not v["snapshot_consent"]:
        raise RegistryError("The visitor did not agree to a snapshot, so none can be taken.")
    if v["snapshot_status"] == "capturing":
        raise RegistryError("A snapshot is already being taken for this visit.")
    camera_id = camera_id or v["gate_camera_id"] or v["entry_camera_id"]
    if camera_id is None or camera_db.get_camera(int(camera_id)) is None:
        raise RegistryError("Choose the camera to take the snapshot from.")
    camera_id = int(camera_id)
    frames, deadline = [], time.time() + LIVE_CAPTURE_SECONDS
    while len(frames) < LIVE_CAPTURE_FRAMES and time.time() < deadline:
        jpg = camera_stream.grab_still(camera_id, timeout=max(1.0, deadline - time.time()))
        if jpg is None:
            break
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            frames.append(img)
    if not frames:
        raise RegistryError(f"No picture from {_camera_name(camera_id)} right now. Is the camera online?")
    # Frames are taken now (the visitor is in front of the camera now); the
    # face check runs in the background and the visit shows the outcome.
    _start_snapshot(visit_id, frames, time.time(), camera_id, "live_capture", "live")
    audit.record("visitor_records.snapshot_capture", principal, f"visit:{visit_id}", camera_id=camera_id, frames=len(frames))
    return {"started": True, "frames_checked": len(frames), "visit": get_visit(visit_id)}


# --------------------------------------------------------------------------- KPIs and analytics

def kpis(day: str | None = None, now: float | None = None) -> dict:
    now = now if now is not None else _now()
    day = _check_day(day) or _today(now)
    month = day[:7]
    long_stay = get_settings()["long_stay_minutes"] * 60
    today = list_visits(date_from=day, date_to=day, limit=100_000, now=now)
    first = datetime.date.fromisoformat(f"{month}-01")
    last = (first.replace(day=28) + datetime.timedelta(days=4)).replace(day=1) - datetime.timedelta(days=1)   # the month's real last day
    month_rows = list_visits(date_from=first.isoformat(), date_to=last.isoformat(), limit=100_000, now=now)
    with _conn() as conn:
        inside_now = conn.execute("SELECT COUNT(*) FROM vm_visits WHERE status = 'in'").fetchone()[0]
    # A visitor is first-time today when today holds their first visit ever,
    # even if they come back again later the same day.
    visitors_today = {r["visitor_id"]: r for r in sorted(today, key=lambda r: r["prior_visits"], reverse=True)}
    first_time = sum(1 for r in visitors_today.values() if r["first_visit"])
    durations = [r["duration_seconds"] for r in today if r["duration_seconds"] is not None]
    waits = [r["waiting_seconds"] for r in today if r["waiting_seconds"] is not None]
    long_rows = [r for r in today if (r["duration_seconds"] or r["elapsed_seconds"] or 0) >= long_stay]
    freq: dict[int, dict] = {}
    for r in month_rows:
        f = freq.setdefault(r["visitor_id"], {"visitor_id": r["visitor_id"], "name": r["visitor_name"], "visitor_code": r["visitor_code"], "visits": 0})
        f["visits"] += 1
    top = sorted(freq.values(), key=lambda f: (-f["visits"], f["name"]))
    return {
        "day": day, "month": month,
        "visitors_today": len(visitors_today), "first_time_today": first_time, "repeat_today": len(visitors_today) - first_time,
        "inside_now": inside_now,
        "checked_out_today": sum(1 for r in today if r["status"] == OUT),
        "pending_today": sum(1 for r in today if r["status"] == PENDING),
        "average_duration_seconds": round(sum(durations) / len(durations)) if durations else None,
        "average_waiting_seconds": round(sum(waits) / len(waits)) if waits else None,
        "waiting_measured": len(waits), "duration_measured": len(durations),
        "long_stay_minutes": long_stay // 60, "long_staying": len(long_rows),
        "long_staying_names": [r["visitor_name"] for r in long_rows][:10],
        "visits_this_month": len(month_rows),
        "repeat_visits_this_month": sum(1 for r in month_rows if not r["first_visit"]),
        "most_frequent": [f for f in top if f["visits"] > 1][:5],
        "inside_names": [r["visitor_name"] for r in today if r["status"] == IN][:10],
        "pending_names": [r["visitor_name"] for r in today if r["status"] == PENDING][:10],
    }


def waiting_now(now: float | None = None) -> list[dict]:
    """Visitors inside who have not met their host yet, longest wait first."""
    now = now if now is not None else _now()
    rows = list_visits(status=IN, limit=1000, now=now)
    out = [{"visit_id": r["id"], "visitor_name": r["visitor_name"], "host_name": r["host_name"],
            "waiting_seconds": now - r["entry_at"]} for r in rows if r["entry_at"] and r["meeting_start_at"] is None]
    return sorted(out, key=lambda w: -w["waiting_seconds"])


def analytics(date_from: str, date_to: str, now: float | None = None) -> dict:
    now = now if now is not None else _now()
    date_from, date_to = _check_day(date_from, "From date"), _check_day(date_to, "To date")
    if not date_from or not date_to or date_from > date_to:
        raise RegistryError("Choose a valid date range.")
    rows = list_visits(date_from=date_from, date_to=date_to, limit=100_000, now=now)
    daily: dict[str, int] = {}
    d = datetime.date.fromisoformat(date_from)
    end = datetime.date.fromisoformat(date_to)
    if (end - d).days > 731:
        raise RegistryError("Choose a range of at most two years.")
    while d <= end:
        daily[d.isoformat()] = 0
        d += datetime.timedelta(days=1)
    weekly: dict[str, int] = {}
    monthly: dict[str, int] = {}
    per_visitor: dict[int, dict] = {}
    purpose: dict[str, int] = {}
    hosts: dict[str, int] = {}
    depts: dict[str, int] = {}
    for r in rows:
        daily[r["visit_date"]] = daily.get(r["visit_date"], 0) + 1
        dt = datetime.date.fromisoformat(r["visit_date"])
        wk = dt - datetime.timedelta(days=dt.weekday())
        weekly[wk.isoformat()] = weekly.get(wk.isoformat(), 0) + 1
        monthly[r["visit_date"][:7]] = monthly.get(r["visit_date"][:7], 0) + 1
        p = per_visitor.setdefault(r["visitor_id"], {"visitor_id": r["visitor_id"], "name": r["visitor_name"],
                                                     "visitor_code": r["visitor_code"], "company": r["visitor_company"], "visits": 0})
        p["visits"] += 1
        key = (r["purpose"] or "").strip().capitalize() or "Not given"
        purpose[key] = purpose.get(key, 0) + 1
        hosts[r["host_name"]] = hosts.get(r["host_name"], 0) + 1
        dep = r["host_department"] or "No department"
        depts[dep] = depts.get(dep, 0) + 1
    buckets = {"1 visit": 0, "2 visits": 0, "3-5 visits": 0, "6+ visits": 0}
    for p in per_visitor.values():
        n = p["visits"]
        buckets["1 visit" if n == 1 else "2 visits" if n == 2 else "3-5 visits" if n <= 5 else "6+ visits"] += 1
    durations = [r["duration_seconds"] for r in rows if r["duration_seconds"] is not None]
    waits = [r["waiting_seconds"] for r in rows if r["waiting_seconds"] is not None]
    pairs = lambda d, k="label": [{k: a, "visits": b} for a, b in sorted(d.items(), key=lambda x: -x[1])]  # noqa: E731
    return {
        "from": date_from, "to": date_to, "total_visits": len(rows), "unique_visitors": len(per_visitor),
        "repeat_visitors": sum(1 for p in per_visitor.values() if p["visits"] > 1),
        "daily": [{"day": k, "visits": v} for k, v in sorted(daily.items())],
        "weekly": [{"week": k, "visits": v} for k, v in sorted(weekly.items())],
        "monthly": [{"month": k, "visits": v} for k, v in sorted(monthly.items())],
        "frequency": [{"label": k, "visitors": v} for k, v in buckets.items()],
        "top_visitors": sorted(per_visitor.values(), key=lambda p: (-p["visits"], p["name"]))[:10],
        "average_duration_seconds": round(sum(durations) / len(durations)) if durations else None,
        "average_waiting_seconds": round(sum(waits) / len(waits)) if waits else None,
        "duration_measured": len(durations), "waiting_measured": len(waits),
        "by_purpose": pairs(purpose), "by_host": pairs(hosts), "by_department": pairs(depts),
    }


# --------------------------------------------------------------------------- export

EXPORT_COLUMNS = [
    ("Visit ID", "visit_code"), ("Visitor ID", "visitor_code"), ("Visitor name", "visitor_name"), ("Company", "visitor_company"),
    ("Visit date", "visit_date"), ("Purpose", "purpose"), ("Person to meet", "host_name"), ("Department", "host_department"),
    ("Status", "status"), ("Registered", "registered_at"), ("Entry", "entry_at"), ("Entry recorded by", "entry_source"),
    ("Host notified", "host_notified_at"), ("Meeting start", "meeting_start_at"), ("Exit", "exit_at"),
    ("Exit recorded by", "exit_source"), ("Waiting (min)", "waiting_seconds"), ("Duration (min)", "duration_seconds"),
    ("First visit", "first_visit"), ("Visits this month", "visits_this_month"), ("Snapshot", "snapshot_status"),
]
_TS_KEYS = {"registered_at", "entry_at", "host_notified_at", "meeting_start_at", "exit_at"}


def _export_value(row, key):
    v = row.get(key)
    if v is None:
        return ""
    if key in _TS_KEYS:
        return datetime.datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M")
    if key in ("waiting_seconds", "duration_seconds"):
        return round(v / 60, 1)
    if key == "first_visit":
        return "Yes" if v else "No"
    if key == "status":
        return {"in": "IN", "out": "OUT", "pending": "Pending", "cancelled": "Cancelled"}.get(v, v)
    return v


def export_rows(**filters) -> tuple[list[str], list[list]]:
    rows = list_visits(limit=100_000, **filters)
    return [c[0] for c in EXPORT_COLUMNS], [[_export_value(r, k) for _h, k in EXPORT_COLUMNS] for r in reversed(rows)]


def to_csv(header, rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        # Cells starting with = + - @ would run as formulas in Excel.
        w.writerow([("'" + c) if isinstance(c, str) and c[:1] in ("=", "+", "-", "@") else c for c in r])
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_xlsx(header, rows, sheet: str = "Visits") -> bytes:
    """A plain .xlsx (one sheet, inline strings), written without extra libraries."""
    def col(n):
        s = ""
        n += 1
        while n:
            n, r = divmod(n - 1, 26)
            s = chr(65 + r) + s
        return s

    def cell(ref, v, style=0):
        if isinstance(v, bool):
            v = "Yes" if v else "No"
        if isinstance(v, (int, float)):
            return f'<c r="{ref}"{f" s=\"{style}\"" if style else ""}><v>{v}</v></c>'
        return f'<c r="{ref}" t="inlineStr"{f" s=\"{style}\"" if style else ""}><is><t xml:space="preserve">{escape(str(v))}</t></is></c>'

    lines = [f'<row r="1">{"".join(cell(f"{col(i)}1", h, 1) for i, h in enumerate(header))}</row>']
    for ri, r in enumerate(rows, start=2):
        lines.append(f'<row r="{ri}">{"".join(cell(f"{col(i)}{ri}", v) for i, v in enumerate(r) if v != "")}</row>')
    sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                 '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                 f'<sheetData>{"".join(lines)}</sheetData></worksheet>')
    files = {
        "[Content_Types].xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                                '<Default Extension="xml" ContentType="application/xml"/>'
                                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                                '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                                '</Types>'),
        "_rels/.rels": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                        '</Relationships>'),
        "xl/workbook.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                            f'<sheets><sheet name="{escape(sheet)}" sheetId="1" r:id="rId1"/></sheets></workbook>'),
        "xl/_rels/workbook.xml.rels": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                                       '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                                       '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                                       '</Relationships>'),
        "xl/styles.xml": ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                          '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                          '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
                          '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                          '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
                          '<cellXfs count="2"><xf/><xf fontId="1" applyFont="1"/></cellXfs></styleSheet>'),
        "xl/worksheets/sheet1.xml": sheet_xml,
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return out.getvalue()


# --------------------------------------------------------------------------- background

def start() -> None:
    def loop():
        while True:
            try:
                purge_expired_media()
            except Exception:
                log.exception("visitor_records: media purge failed")
            if lifecycle.wait(PURGE_INTERVAL_SECONDS):
                return

    threading.Thread(target=loop, daemon=True, name="visitor-records-purge").start()
