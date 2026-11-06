"""
whatsapp.py

SMC Connect: WhatsApp Business notifications through the Meta WhatsApp Cloud API (Graph API).

How an organization connects (nothing is ever assumed connected; "connected" is stored only after
Meta answered for the phone number with that organization's credentials):

  Embedded Signup   the organization's admin clicks Connect, signs in to Meta in Meta's own popup
                    and picks or creates the WhatsApp Business Account and number. SMC receives a
                    30-second code, exchanges it on the server for a business token, stores the
                    token ENCRYPTED (credential_store.py), subscribes its Meta app to the account's
                    webhooks and, with the PIN the admin chose, registers the number.
                    Needs META_APP_ID, META_APP_SECRET, META_EMBEDDED_SIGNUP_CONFIG_ID and
                    SMC_CREDENTIALS_KEY on the server (docs/smc-connect-whatsapp-setup.md).
  Manual            a system-user token in the environment (WHATSAPP_ACCESS_TOKEN,
                    WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_BUSINESS_ACCOUNT_ID), checked with Verify.
  Demo mode         neither is set up: the page works, nothing is sent and nothing pretends to be.

Tokens never leave the server: no API response, log line or history row contains one.

Organizations. Every table carries org_id and every query filters on it, so connections,
credentials, recipients, rules, templates and the message log of one organization are never read
or changed for another. The organization is this deployment's (SMC_ORG_ID, default "default"):
an SMC installation serves one organization, and the Meta webhook (one per Meta app) is routed to
the right organization by the phone number / business account it is about.

What gets sent (the dispatcher only READS the other modules; cameras, AI, attendance and visitors
are not touched and never wait for WhatsApp):
  intrusion / loitering / late_arrival   new rows in the alerts table (alerts.py)
  visitor                               new visitor check-ins (vm_visits.entry_at)
  attendance / footfall / daily_summary  a daily report at each rule's send time, from
                                         insights.business_overview (the dashboard's numbers)

Delivery. A rule's message goes only to recipients who opted in, and only in a form Meta accepts:
an APPROVED message template linked to the rule (required for business-initiated messages), or
free text while the recipient is inside the 24-hour window that opens when they message the
business number. Anything else is logged as not_sent with the reason; Meta is not called just to
fail. Messages are queued and sent by a background worker, retried with backoff on network errors,
Meta 5xx and rate limits, and de-duplicated per (event, source, recipient). Every row shows its real
outcome: queued / retrying, sent (accepted by Meta), delivered / read (Meta's webhook), failed
(Meta's error) or not_sent (the reason).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from app import config, credential_store, db, lifecycle

log = logging.getLogger("whatsapp")

DB_PATH = config.DB_PATH
GRAPH = "https://graph.facebook.com"
DEFAULT_API_VERSION = "v24.0"
POLL_SECONDS = 15            # how often the dispatcher looks for new alerts / check-ins / reports
SEND_POLL_SECONDS = 3        # how often the sender looks at the queue
MAX_ATTEMPTS = 5
RETRY_BACKOFF_SECONDS = (30, 120, 600, 1800)
SENDING_STALE_SECONDS = 300  # a row left "sending" by a crash is retried after this
WINDOW_SECONDS = 24 * 3600   # Meta's customer-service window

ROLES = ("Admin", "Security Manager", "HR Manager", "Reception")
PRIORITIES = ("High", "Normal", "Low")
STATUSES = ("queued", "retrying", "sent", "delivered", "read", "failed", "not_sent")
OPT_IN_METHODS = {
    "written": "Written consent",
    "web_form": "Sign-up / web form",
    "verbal": "Verbal consent, recorded by an admin",
    "whatsapp_reply": "Replied START on WhatsApp",
}
OPT_OUT_WORDS = {"STOP", "UNSUBSCRIBE", "STOP ALL"}
OPT_IN_WORDS = {"START", "SUBSCRIBE"}

# Meta error codes worth retrying: throttling / temporary unavailability.
# (developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes)
TRANSIENT_CODES = {1, 2, 4, 17, 341, 80007, 130429, 131000, 131016, 131056, 133004}
# Meta OAuthException: the access token is invalid, expired or revoked.
TOKEN_REJECTED = 190

# The notification rules: key -> (label, kind, default priority, default text, sample values).
EVENTS = {
    "intrusion": ("Intrusion Detection", "event", "High",
                  "🚨 *Intrusion detected*\nArea: {area}\nZone: {zone}\nCamera: {camera}\nTime: {time}\n{message}\n\nReview the video in SMC: {link}",
                  {"area": "Reception", "zone": "Reception Entry Gate", "camera": "CAM2", "time": "02 Nov 2026, 11:39 PM",
                   "message": "1 person inside Reception Entry Gate", "link": "SMC > Analytics > Intrusion"}),
    "loitering": ("Loitering Detection", "event", "Normal",
                  "⚠️ *Loitering detected*\nZone: {zone} ({area})\nCamera: {camera}\nTime: {time}\n{message}",
                  {"area": "Lobby", "zone": "Waiting Area", "camera": "CAM4", "time": "03 Nov 2026, 10:15 AM",
                   "message": "Someone stayed in Waiting Area for 15 minutes (limit 15 minutes)"}),
    "visitor": ("Visitor Management", "event", "Normal",
                "👋 *Visitor checked in*\n{visitor} ({company})\nTo meet: {host}\nPurpose: {purpose}\nTime: {time}",
                {"visitor": "Rahul Mehta", "company": "Acme Pvt Ltd", "host": "Ravi Kumar", "purpose": "Meeting",
                 "time": "03 Nov 2026, 11:02 AM"}),
    "attendance": ("Employee Attendance", "report", "Normal",
                   "📋 *Attendance · {date}*\nPresent: {present} of {total} ({attendance_pct}%)\nLate: {late}\nAbsent: {absent}",
                   {"date": "03 Nov 2026", "present": 13, "total": 15, "attendance_pct": 87, "late": 2, "absent": 2}),
    "late_arrival": ("Late Arrivals", "event", "Low",
                     "⏰ *Late arrival*\n{message}\nCamera: {camera}\nTime: {time}",
                     {"message": "Sumit Verma first seen at 10:42 AM", "camera": "Cam3", "time": "03 Nov 2026, 10:42 AM"}),
    "footfall": ("Footfall Reports", "report", "Low",
                 "🚶 *Footfall · {date}*\nPeople entering: {entries}\nEmployees / others: {employee_entries} / {visitor_entries}\nPeak hour: {peak_hour}",
                 {"date": "03 Nov 2026", "entries": 142, "employee_entries": 98, "visitor_entries": 44, "peak_hour": "10 AM"}),
    "daily_summary": ("Daily Business Summary", "report", "Normal",
                      "📊 *SMC daily summary · {date}*\n{summary}",
                      {"date": "03 Nov 2026", "summary": "• Workforce: 13 of 15 present, 2 late.\n• Visitors: 6 today.\n• Security: 1 alert raised, 0 still open."}),
}
ALERT_EVENT_RULE = {"Intrusion Detected": "intrusion", "Loitering Detected": "loitering", "Late Arrival": "late_arrival"}
PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
META_PARAM = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")


class NotConfigured(Exception):
    """No usable Meta credentials for this organization."""


class MetaError(Exception):
    """Meta answered with an error, or could not be reached. The message is Meta's (or the network's)."""

    def __init__(self, message: str, code: int | None = None, http_status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.transient = transient


def org_id() -> str:
    """This deployment's organization (SMC_ORG_ID); every row is scoped to it."""
    return (os.environ.get("SMC_ORG_ID") or "default").strip()[:64] or "default"


def api_version() -> str:
    return os.environ.get("WHATSAPP_API_VERSION") or DEFAULT_API_VERSION


# ---- Graph API transport ------------------------------------------------------------------------

class Graph:
    """Every Meta call goes through call(), so tests replace it. The token is passed per call
    (each organization has its own) and only ever put in the Authorization header."""

    def call(self, method: str, path: str, token: str | None, body: dict | None = None, query: dict | None = None) -> dict:
        url = f"{GRAPH}/{api_version()}/{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req, timeout=15) as res:  # noqa: S310 - fixed Graph host
                return json.loads(res.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            err = {}
            try:
                err = json.loads(e.read().decode()).get("error", {}) or {}
            except ValueError:
                pass
            code = err.get("code")
            transient = e.code >= 500 or e.code == 429 or code in TRANSIENT_CODES
            msg = err.get("error_user_msg") or err.get("message") or f"Meta answered {e.code}"
            raise MetaError(msg, code=code, http_status=e.code, transient=transient) from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise MetaError(f"Could not reach Meta: {e}", transient=True) from e


graph = Graph()


# ---- storage ------------------------------------------------------------------------------------

def _conn():
    return db.connect(DB_PATH, row_factory=sqlite3.Row)


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def init_db() -> None:
    """Creates the SMC Connect tables and upgrades the first version's tables in place (their rows
    are kept and assigned to this deployment's organization)."""
    org = org_id()
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS wa_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_business_accounts (
                           id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, waba_id TEXT NOT NULL,
                           business_id TEXT, name TEXT, review_status TEXT,
                           token_enc TEXT, token_source TEXT NOT NULL,
                           created_at REAL NOT NULL, updated_at REAL NOT NULL, UNIQUE (org_id, waba_id))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_connections (
                           org_id TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'manual', business_account_id INTEGER,
                           phone_number_id TEXT, status TEXT NOT NULL DEFAULT 'not_connected', entered_number TEXT,
                           display_phone_number TEXT, verified_name TEXT, quality_rating TEXT, last_error TEXT,
                           checked_at REAL, connected_at REAL, updated_at REAL)""")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_wa_connections_phone ON wa_connections (phone_number_id) "
                     "WHERE phone_number_id IS NOT NULL")
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_templates (
                           id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, waba_id TEXT NOT NULL,
                           name TEXT NOT NULL, language TEXT NOT NULL, category TEXT, status TEXT NOT NULL,
                           body TEXT NOT NULL DEFAULT '', parameter_format TEXT NOT NULL DEFAULT 'POSITIONAL',
                           params TEXT NOT NULL DEFAULT '[]', synced_at REAL NOT NULL, UNIQUE (org_id, name, language))""")

        _upgrade_recipients(conn, org)
        _upgrade_rules(conn, org)

        conn.execute("""CREATE TABLE IF NOT EXISTS wa_messages (
                           id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, ts REAL NOT NULL, event_key TEXT NOT NULL,
                           recipient_id INTEGER, recipient_name TEXT, phone TEXT NOT NULL, body TEXT NOT NULL,
                           status TEXT NOT NULL, wamid TEXT, error TEXT, is_test INTEGER NOT NULL DEFAULT 0,
                           source_ref TEXT, status_at REAL, message_type TEXT, payload TEXT,
                           attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL)""")
        cols = _columns(conn, "wa_messages")
        for col, ddl in (("org_id", "TEXT"), ("message_type", "TEXT"), ("payload", "TEXT"),
                         ("attempts", "INTEGER NOT NULL DEFAULT 0"), ("next_attempt_at", "REAL")):
            if col not in cols:
                conn.execute(f"ALTER TABLE wa_messages ADD COLUMN {col} {ddl}")
        conn.execute("UPDATE wa_messages SET org_id = ? WHERE org_id IS NULL", (org,))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_messages_ts ON wa_messages (org_id, ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_messages_wamid ON wa_messages (wamid)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_messages_due ON wa_messages (status, next_attempt_at)")
        try:
            # One notification per (event, source, recipient): a restart or a second pass can't repeat it.
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_wa_messages_once ON wa_messages (org_id, event_key, source_ref, recipient_id) "
                         "WHERE is_test = 0 AND source_ref IS NOT NULL")
        except sqlite3.IntegrityError:
            log.warning("smc-connect: older duplicate notifications exist; the duplicate guard index was not created")

        # The first version kept the connection as a JSON setting.
        old = conn.execute("SELECT value FROM wa_settings WHERE key = 'connection'").fetchone()
        if old:
            c = json.loads(old["value"]) or {}
            conn.execute("INSERT OR IGNORE INTO wa_connections (org_id, mode, status, entered_number, display_phone_number, verified_name, "
                         "quality_rating, last_error, checked_at, connected_at, updated_at) VALUES (?, 'manual', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (org, c.get("status") or "not_connected", c.get("entered_number"), c.get("display_phone_number"),
                          c.get("verified_name"), c.get("quality_rating"), c.get("last_error"), c.get("checked_at"),
                          c.get("connected_at"), time.time()))
            conn.execute("DELETE FROM wa_settings WHERE key = 'connection'")

        for key, (_label, kind, priority, _t, _s) in EVENTS.items():
            conn.execute("INSERT OR IGNORE INTO wa_rules (org_id, event_key, enabled, priority, preferences, updated_at) "
                         "VALUES (?, ?, 0, ?, ?, ?)", (org, key, priority, json.dumps(_default_prefs(kind)), time.time()))


def _upgrade_recipients(conn, org: str) -> None:
    ddl = """CREATE TABLE IF NOT EXISTS {name} (
               id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL,
               role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL,
               opt_in_status TEXT NOT NULL DEFAULT 'pending', opt_in_method TEXT, opt_in_at REAL, opt_in_by TEXT,
               opted_out_at REAL, last_inbound_at REAL, UNIQUE (org_id, phone))"""
    if not _table(conn, "wa_recipients"):
        conn.execute(ddl.format(name="wa_recipients"))
        return
    if "org_id" in _columns(conn, "wa_recipients"):
        return
    # First version: phone was unique across the whole table. Rebuild with per-organization
    # uniqueness and opt-in columns, keeping ids (rules refer to them). Existing recipients start as
    # "pending": nobody recorded their consent, so nothing is sent to them until an admin does.
    conn.execute("DROP TABLE IF EXISTS wa_recipients_v2")
    conn.execute(ddl.format(name="wa_recipients_v2"))
    conn.execute("INSERT INTO wa_recipients_v2 (id, org_id, name, phone, role, active, created_at) "
                 "SELECT id, ?, name, phone, role, active, created_at FROM wa_recipients", (org,))
    conn.execute("DROP TABLE wa_recipients")
    conn.execute("ALTER TABLE wa_recipients_v2 RENAME TO wa_recipients")


def _upgrade_rules(conn, org: str) -> None:
    ddl = """CREATE TABLE IF NOT EXISTS {name} (
               org_id TEXT NOT NULL, event_key TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
               recipient_ids TEXT NOT NULL DEFAULT '[]', priority TEXT NOT NULL, preferences TEXT NOT NULL DEFAULT '{{}}',
               template TEXT, meta_template_name TEXT, meta_template_language TEXT, meta_params TEXT,
               updated_at REAL, PRIMARY KEY (org_id, event_key))"""
    if not _table(conn, "wa_rules"):
        conn.execute(ddl.format(name="wa_rules"))
        return
    if "org_id" in _columns(conn, "wa_rules"):
        return
    conn.execute("DROP TABLE IF EXISTS wa_rules_v2")
    conn.execute(ddl.format(name="wa_rules_v2"))
    conn.execute("INSERT INTO wa_rules_v2 (org_id, event_key, enabled, recipient_ids, priority, preferences, template, updated_at) "
                 "SELECT ?, event_key, enabled, recipient_ids, priority, preferences, template, updated_at FROM wa_rules", (org,))
    conn.execute("DROP TABLE wa_rules")
    conn.execute("ALTER TABLE wa_rules_v2 RENAME TO wa_rules")


def _default_prefs(kind: str) -> dict:
    prefs = {"quiet_from": "", "quiet_to": "", "cooldown_minutes": 0 if kind == "report" else 5}
    if kind == "report":
        prefs["send_at"] = "20:00"
    return prefs


# Dispatcher bookkeeping (which alerts / check-ins / reports were already looked at). The alerts
# and visitor tables belong to this deployment, so this is per deployment too.
def _get(key: str, default=None):
    with _conn() as conn:
        row = conn.execute("SELECT value FROM wa_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def _set(key: str, value) -> None:
    with _conn() as conn:
        conn.execute("INSERT INTO wa_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps(value)))


# ---- phone numbers ------------------------------------------------------------------------------

def normalize_phone(country_code: str, number: str) -> str:
    """Digits only, E.164 without '+': country code 1-3 digits, national number 6-14 digits."""
    cc = re.sub(r"\D", "", country_code or "")
    nn = re.sub(r"\D", "", number or "").lstrip("0")
    if not 1 <= len(cc) <= 3:
        raise ValueError("Enter a country code, like +91")
    if not 6 <= len(nn) <= 14:
        raise ValueError("Enter the phone number without the country code (6 to 14 digits)")
    return cc + nn


def mask(phone: str | None) -> str:
    if not phone:
        return ""
    return f"+{phone[:-6]}••••{phone[-2:]}" if len(phone) > 8 else f"+{phone}"


# ---- credentials and connection -----------------------------------------------------------------

# Manual setup: the environment variables it needs (WHATSAPP_BUSINESS_ACCOUNT_ID is optional).
MANUAL_ENV = ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID")


def manual_missing() -> list[str]:
    return [n for n in MANUAL_ENV if not os.environ.get(n, "").strip()]


def _env_credentials() -> dict | None:
    token, pnid = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip(), os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    if not (token and pnid):
        return None
    return {"token": token, "phone_number_id": pnid, "waba_id": os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID", "").strip(), "mode": "manual"}


def embedded_signup_config() -> dict:
    """What the browser needs to open Meta's Embedded Signup popup. The app id and configuration
    id are public by design (they are in Meta's JavaScript); the app secret never leaves here."""
    missing = [n for n in ("META_APP_ID", "META_APP_SECRET", "META_EMBEDDED_SIGNUP_CONFIG_ID") if not os.environ.get(n)]
    if not credential_store.available():
        missing.append(credential_store.ENV)
    return {"available": not missing, "missing": missing, "app_id": os.environ.get("META_APP_ID") or None,
            "config_id": os.environ.get("META_EMBEDDED_SIGNUP_CONFIG_ID") or None, "graph_version": api_version()}


def _row(org: str | None = None) -> dict:
    org = org or org_id()
    with _conn() as conn:
        r = conn.execute("SELECT * FROM wa_connections WHERE org_id = ?", (org,)).fetchone()
        acct = None
        if r and r["business_account_id"]:
            acct = conn.execute("SELECT * FROM wa_business_accounts WHERE id = ? AND org_id = ?", (r["business_account_id"], org)).fetchone()
    c = dict(r) if r else {"org_id": org, "mode": "manual", "status": "not_connected"}
    c["account"] = dict(acct) if acct else None
    return c


def _save(org: str, **fields) -> None:
    fields["updated_at"] = time.time()
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO wa_connections (org_id) VALUES (?)", (org,))
        sets = ", ".join(f"{k} = ?" for k in fields)
        conn.execute(f"UPDATE wa_connections SET {sets} WHERE org_id = ?", (*fields.values(), org))


def _credentials(c: dict) -> dict:
    """The organization's token and ids: its Embedded Signup token (decrypted), else the
    environment's manual credentials. Raises NotConfigured / credential_store.Undecryptable."""
    if c.get("mode") == "embedded_signup":
        acct = c.get("account") or {}
        if not acct.get("token_enc"):
            raise NotConfigured("WhatsApp is not connected (sign in with Meta again)")
        return {"token": credential_store.decrypt(acct["token_enc"]), "phone_number_id": c.get("phone_number_id"),
                "waba_id": acct.get("waba_id"), "mode": "embedded_signup"}
    env = _env_credentials()
    if env is None:
        raise NotConfigured("WhatsApp Cloud API credentials are not set on the server")
    return env


def _has_credentials(c: dict) -> bool:
    if c.get("mode") == "embedded_signup":
        return bool((c.get("account") or {}).get("token_enc"))
    return _env_credentials() is not None


def _state(status: str, demo: bool) -> str:
    """The four states the page shows: not_configured (no credentials and no Embedded Signup on the
    server), configured (can be checked, not confirmed by Meta), connected (Meta confirmed it at the
    last check), connection_failed (Meta, or the network, refused the last check or send)."""
    if demo:
        return "not_configured"
    return {"connected": "connected", "failed": "connection_failed"}.get(status, "configured")


def connection() -> dict:
    """What the page shows. Never says connected unless Meta confirmed it with the credentials the
    server has now; never contains a token."""
    c = _row()
    es = embedded_signup_config()
    configured = _has_credentials(c)
    env = _env_credentials()
    demo = not configured and not es["available"]
    status = c.get("status") or "not_connected"
    last_error = c.get("last_error")
    if status == "connected" and (not configured or (c.get("mode") != "embedded_signup" and c.get("phone_number_id") != env["phone_number_id"])):
        # Meta confirmed credentials the server no longer has: unconfirmed until checked again.
        status, last_error = "not_connected", "The WhatsApp credentials on the server changed since Meta confirmed them. Check the connection again."
    if demo:
        status = "not_configured"
    connected = status == "connected"
    acct = c.get("account") or {}
    return {
        "state": _state(status, demo),        # not_configured | configured | connected | connection_failed
        "status": status,                     # not_configured | not_connected | pending | connected | failed | disconnected
        "mode": c.get("mode") or "manual",    # manual (environment) | embedded_signup
        "configured": configured,
        "demo_mode": demo,
        "setup": {"embedded_signup": es["available"], "embedded_signup_missing": es["missing"],
                  "manual": env is not None, "manual_missing": manual_missing()},
        "entered_number": c.get("entered_number"),
        "business_number": c.get("display_phone_number") if connected else None,
        "verified_name": c.get("verified_name") if connected else None,
        "quality_rating": c.get("quality_rating") if connected else None,
        "business_account": ({"name": acct.get("name"), "review_status": acct.get("review_status")} if connected and acct else None),
        "checked_at": c.get("checked_at"),
        "connected_at": c.get("connected_at") if connected else None,
        "last_error": last_error,
    }


def start_connect(country_code: str, number: str) -> dict:
    """Manual setup, step 1: the number the business wants to connect. Not connected yet."""
    phone = normalize_phone(country_code, number)
    _save(org_id(), mode="manual", status="pending", entered_number=phone, last_error=None, checked_at=time.time())
    return connection()


def _upsert_account(org: str, waba_id: str, *, name=None, review_status=None, token_enc=None, token_source: str,
                    business_id=None) -> int:
    now = time.time()
    with _conn() as conn:
        conn.execute("INSERT OR IGNORE INTO wa_business_accounts (org_id, waba_id, token_source, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                     (org, waba_id, token_source, now, now))
        conn.execute("UPDATE wa_business_accounts SET name = COALESCE(?, name), review_status = COALESCE(?, review_status), "
                     "business_id = COALESCE(?, business_id), token_enc = ?, token_source = ?, updated_at = ? WHERE org_id = ? AND waba_id = ?",
                     (name, review_status, business_id, token_enc, token_source, now, org, waba_id))
        return conn.execute("SELECT id FROM wa_business_accounts WHERE org_id = ? AND waba_id = ?", (org, waba_id)).fetchone()["id"]


def _fail(org: str, message: str) -> dict:
    _save(org, status="failed", last_error=message, checked_at=time.time())
    log.warning("smc-connect: connection check failed: %s", message)
    return connection()


def verify() -> dict:
    """Asks Meta about the organization's phone number (and business account). Connected only when
    Meta answered with these credentials and, in manual setup, its number is the one entered."""
    org = org_id()
    c = _row(org)
    try:
        cred = _credentials(c)
    except NotConfigured as e:
        missing = manual_missing() if c.get("mode") != "embedded_signup" else []
        _save(org, status="not_connected", checked_at=time.time(),
              last_error=f"Not configured: {e}. Set {' and '.join(missing)} in backend/.env, then restart SMC." if missing else f"{e}.")
        return connection()
    except credential_store.Undecryptable as e:
        return _fail(org, f"{e}. Connect again with Meta.")
    try:
        info = graph.call("GET", cred["phone_number_id"], cred["token"],
                          query={"fields": "display_phone_number,verified_name,quality_rating,code_verification_status"})
    except MetaError as e:
        return _fail(org, str(e))
    account = None
    if cred.get("waba_id"):
        try:
            account = graph.call("GET", cred["waba_id"], cred["token"], query={"fields": "name,account_review_status"})
        except MetaError as e:
            log.info("smc-connect: business account details unavailable: %s", e)
    meta_digits = re.sub(r"\D", "", info.get("display_phone_number") or "")
    entered = c.get("entered_number")
    if cred["mode"] == "manual" and entered and meta_digits and meta_digits != entered:
        return _fail(org, f"Meta's number for this account is {mask(meta_digits)}, not {mask(entered)}.")
    acct_id = c.get("business_account_id")
    if cred.get("waba_id"):
        acct_id = _upsert_account(org, cred["waba_id"], name=(account or {}).get("name"),
                                  review_status=(account or {}).get("account_review_status"),
                                  token_enc=(c.get("account") or {}).get("token_enc") if cred["mode"] == "embedded_signup" else None,
                                  token_source="embedded_signup" if cred["mode"] == "embedded_signup" else "environment")
    try:
        _save(org, status="connected", last_error=None, phone_number_id=cred["phone_number_id"], business_account_id=acct_id,
              display_phone_number=info.get("display_phone_number"), verified_name=info.get("verified_name"),
              quality_rating=info.get("quality_rating"), checked_at=time.time(), entered_number=entered or meta_digits,
              connected_at=c.get("connected_at") if c.get("status") == "connected" else time.time())
    except sqlite3.IntegrityError:
        return _fail(org, "This WhatsApp number is already connected to another organization.")
    return connection()


def complete_embedded_signup(code: str, waba_id: str, phone_number_id: str, pin: str | None = None) -> dict:
    """Finishes Meta Embedded Signup on the server: code -> business token, check that the number
    really belongs to the authorized account, subscribe the app to its webhooks, register the
    number (when a PIN is given), store the token encrypted, then sync templates."""
    cfg = embedded_signup_config()
    if not cfg["available"]:
        raise NotConfigured("Embedded Signup is not set up on the server: " + ", ".join(cfg["missing"]))
    code, waba_id, phone_number_id, pin = (code or "").strip(), (waba_id or "").strip(), (phone_number_id or "").strip(), (pin or "").strip()
    if not code or len(code) > 2048:
        raise ValueError("Meta did not return a sign-up code")
    if not waba_id.isdigit() or not phone_number_id.isdigit() or len(waba_id) > 32 or len(phone_number_id) > 32:
        raise ValueError("Meta did not return a WhatsApp Business Account and phone number")
    if pin and not re.fullmatch(r"\d{6}", pin):
        raise ValueError("The PIN must be 6 digits")
    org = org_id()
    _save(org, status="pending", last_error=None, checked_at=time.time())
    try:
        token = graph.call("GET", "oauth/access_token", None, query={
            "client_id": os.environ["META_APP_ID"], "client_secret": os.environ["META_APP_SECRET"], "code": code})["access_token"]
    except (MetaError, KeyError) as e:
        return _fail(org, f"Meta did not accept the sign-up code (it is valid for 30 seconds; try again): {e}")
    try:
        numbers = graph.call("GET", f"{waba_id}/phone_numbers", token,
                             query={"fields": "id,display_phone_number,verified_name,quality_rating"}).get("data") or []
        phone = next((p for p in numbers if str(p.get("id")) == phone_number_id), None)
        if phone is None:
            return _fail(org, "That phone number is not in the WhatsApp Business Account you authorized.")
        try:
            account = graph.call("GET", waba_id, token, query={"fields": "name,account_review_status"})
        except MetaError:
            account = {}
        graph.call("POST", f"{waba_id}/subscribed_apps", token)
        if pin:
            graph.call("POST", f"{phone_number_id}/register", token, {"messaging_product": "whatsapp", "pin": pin})
    except MetaError as e:
        return _fail(org, str(e))
    acct_id = _upsert_account(org, waba_id, name=account.get("name"), review_status=account.get("account_review_status"),
                              token_enc=credential_store.encrypt(token), token_source="embedded_signup")
    digits = re.sub(r"\D", "", phone.get("display_phone_number") or "")
    try:
        _save(org, mode="embedded_signup", status="connected", last_error=None, business_account_id=acct_id,
              phone_number_id=phone_number_id, display_phone_number=phone.get("display_phone_number"),
              verified_name=phone.get("verified_name"), quality_rating=phone.get("quality_rating"),
              entered_number=digits, checked_at=time.time(), connected_at=time.time())
    except sqlite3.IntegrityError:
        _upsert_account(org, waba_id, token_enc=None, token_source="embedded_signup")
        return _fail(org, "This WhatsApp number is already connected to another organization.")
    log.info("smc-connect: connected through Embedded Signup (number %s)", mask(digits))
    try:
        sync_templates()
    except (MetaError, NotConfigured) as e:
        log.info("smc-connect: template sync after sign-up failed: %s", e)
    return connection()


def disconnect() -> dict:
    """Stops sending. An Embedded Signup token is deleted (connecting again asks Meta again)."""
    org = org_id()
    c = _row(org)
    if c.get("mode") == "embedded_signup" and c.get("account"):
        _upsert_account(org, c["account"]["waba_id"], token_enc=None, token_source="embedded_signup")
    _save(org, status="disconnected", last_error=None, checked_at=time.time())
    return connection()


def is_connected() -> bool:
    return connection()["status"] == "connected"


# ---- Meta message templates -----------------------------------------------------------------------

def _template_body(components: list) -> str:
    return next((c.get("text") or "" for c in components or [] if (c.get("type") or "").upper() == "BODY"), "")


def _template_params(body: str) -> tuple[str, list[str]]:
    names = list(dict.fromkeys(META_PARAM.findall(body)))
    if names and all(n.isdigit() for n in names):
        return "POSITIONAL", sorted(names, key=int)
    return ("NAMED" if names else "POSITIONAL"), names


def sync_templates() -> list[dict]:
    """Reads the organization's message templates from Meta (all statuses; only APPROVED ones can be
    used) and replaces the local copy."""
    org = org_id()
    cred = _credentials(_row(org))
    if not cred.get("waba_id"):
        raise NotConfigured("The WhatsApp Business Account id is not known (WHATSAPP_BUSINESS_ACCOUNT_ID)")
    found, after = [], None
    for _page in range(10):
        q = {"fields": "name,language,status,category,components,parameter_format", "limit": 100}
        if after:
            q["after"] = after
        res = graph.call("GET", f"{cred['waba_id']}/message_templates", cred["token"], query=q)
        found += res.get("data") or []
        after = ((res.get("paging") or {}).get("cursors") or {}).get("after")
        if not after or not (res.get("paging") or {}).get("next"):
            break
    now = time.time()
    with _conn() as conn:
        conn.execute("DELETE FROM wa_templates WHERE org_id = ?", (org,))
        for t in found:
            body = _template_body(t.get("components"))
            fmt, params = _template_params(body)
            conn.execute("INSERT OR REPLACE INTO wa_templates (org_id, waba_id, name, language, category, status, body, parameter_format, params, synced_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (org, cred["waba_id"], t.get("name"), t.get("language"), t.get("category"), (t.get("status") or "").upper(),
                          body, (t.get("parameter_format") or fmt).upper(), json.dumps(params), now))
    return list_templates()


def _template(r) -> dict:
    return {"name": r["name"], "language": r["language"], "category": r["category"], "status": r["status"], "body": r["body"],
            "parameter_format": r["parameter_format"], "params": json.loads(r["params"]), "synced_at": r["synced_at"]}


def list_templates() -> list[dict]:
    with _conn() as conn:
        return [_template(r) for r in conn.execute("SELECT * FROM wa_templates WHERE org_id = ? ORDER BY name, language", (org_id(),))]


def _get_template(name: str, language: str) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM wa_templates WHERE org_id = ? AND name = ? AND language = ?", (org_id(), name, language)).fetchone()
    return _template(r) if r else None


def _param_text(v) -> str:
    """Meta refuses template parameters with new lines, tabs or more than 4 spaces in a row."""
    s = re.sub(r"\s*\n\s*", " · ", str(v if v is not None else "")).replace("\t", " ")
    s = re.sub(r" {4,}", "   ", s).strip()
    return s[:1000] or "-"


def _fill_template(t: dict, fields: list[str], values: dict) -> tuple[dict, str]:
    """(Meta template object, the text the recipient sees) for these SMC values."""
    texts = [_param_text(values.get(f)) for f in fields]
    params = []
    for name, text in zip(t["params"], texts):
        p = {"type": "text", "text": text}
        if t["parameter_format"] == "NAMED":
            p["parameter_name"] = name
        params.append(p)
    by_name = dict(zip(t["params"], texts))
    shown = META_PARAM.sub(lambda m: by_name.get(m.group(1), m.group(0)), t["body"])
    obj = {"name": t["name"], "language": {"code": t["language"]}}
    if params:
        obj["components"] = [{"type": "body", "parameters": params}]
    return obj, shown


# ---- recipients and opt-in ------------------------------------------------------------------------

def _recipient(r) -> dict:
    now = time.time()
    return {"id": r["id"], "name": r["name"], "phone": r["phone"], "phone_masked": mask(r["phone"]), "role": r["role"],
            "active": bool(r["active"]), "created_at": r["created_at"],
            "opt_in": {"status": r["opt_in_status"], "method": r["opt_in_method"], "method_label": OPT_IN_METHODS.get(r["opt_in_method"]),
                       "at": r["opt_in_at"], "by": r["opt_in_by"], "opted_out_at": r["opted_out_at"]},
            "window_open": bool(r["last_inbound_at"] and now - r["last_inbound_at"] < WINDOW_SECONDS),
            "last_inbound_at": r["last_inbound_at"]}


def list_recipients() -> list[dict]:
    with _conn() as conn:
        return [_recipient(r) for r in conn.execute("SELECT * FROM wa_recipients WHERE org_id = ? ORDER BY name", (org_id(),))]


def get_recipient(rid: int) -> dict | None:
    with _conn() as conn:
        r = conn.execute("SELECT * FROM wa_recipients WHERE id = ? AND org_id = ?", (rid, org_id())).fetchone()
    return _recipient(r) if r else None


def add_recipient(name: str, country_code: str, number: str, role: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Enter the recipient's name")
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
    phone = normalize_phone(country_code, number)
    try:
        with _conn() as conn:
            cur = conn.execute("INSERT INTO wa_recipients (org_id, name, phone, role, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                               (org_id(), name[:80], phone, role, time.time()))
            rid = cur.lastrowid
    except sqlite3.IntegrityError as e:
        raise ValueError("This phone number is already a recipient") from e
    return get_recipient(rid)


def update_recipient(rid: int, **fields) -> dict | None:
    sets, vals = [], []
    if fields.get("name") is not None:
        if not fields["name"].strip():
            raise ValueError("Enter the recipient's name")
        sets.append("name = ?"), vals.append(fields["name"].strip()[:80])
    if fields.get("role") is not None:
        if fields["role"] not in ROLES:
            raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
        sets.append("role = ?"), vals.append(fields["role"])
    if fields.get("active") is not None:
        sets.append("active = ?"), vals.append(int(bool(fields["active"])))
    if sets:
        with _conn() as conn:
            conn.execute(f"UPDATE wa_recipients SET {', '.join(sets)} WHERE id = ? AND org_id = ?", (*vals, rid, org_id()))
    return get_recipient(rid)


def record_opt_in(rid: int, method: str, recorded_by: str) -> dict | None:
    """Records that the recipient agreed to receive WhatsApp notifications, how, when and who
    recorded it. Meta's policy requires this before business-initiated messages."""
    if method not in OPT_IN_METHODS or method == "whatsapp_reply":
        raise ValueError("Choose how the recipient gave consent")
    with _conn() as conn:
        conn.execute("UPDATE wa_recipients SET opt_in_status = 'opted_in', opt_in_method = ?, opt_in_at = ?, opt_in_by = ?, opted_out_at = NULL "
                     "WHERE id = ? AND org_id = ?", (method, time.time(), (recorded_by or "")[:120], rid, org_id()))
    return get_recipient(rid)


def record_opt_out(rid: int, recorded_by: str) -> dict | None:
    with _conn() as conn:
        conn.execute("UPDATE wa_recipients SET opt_in_status = 'opted_out', opted_out_at = ?, opt_in_by = ? WHERE id = ? AND org_id = ?",
                     (time.time(), (recorded_by or "")[:120], rid, org_id()))
    return get_recipient(rid)


def delete_recipient(rid: int) -> None:
    org = org_id()
    with _conn() as conn:
        conn.execute("DELETE FROM wa_recipients WHERE id = ? AND org_id = ?", (rid, org))
        for r in conn.execute("SELECT event_key, recipient_ids FROM wa_rules WHERE org_id = ?", (org,)).fetchall():
            ids = [i for i in json.loads(r["recipient_ids"]) if i != rid]
            conn.execute("UPDATE wa_rules SET recipient_ids = ? WHERE org_id = ? AND event_key = ?", (json.dumps(ids), org, r["event_key"]))


# ---- rules and message texts ----------------------------------------------------------------------

def _hhmm_ok(v: str) -> bool:
    return v == "" or bool(re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v or ""))


def render(template: str, values: dict) -> str:
    """Fills {field} placeholders; an unknown field is left as written so nothing is invented."""
    return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values and values[m.group(1)] is not None else m.group(0), template)


def _rule(r) -> dict:
    label, kind, _default_priority, default_template, sample = EVENTS[r["event_key"]]
    template = r["template"] or default_template
    meta = None
    if r["meta_template_name"]:
        t = _get_template(r["meta_template_name"], r["meta_template_language"])
        fields = json.loads(r["meta_params"] or "[]")
        meta = {"name": r["meta_template_name"], "language": r["meta_template_language"], "params": fields,
                "status": t["status"] if t else "MISSING", "usable": bool(t and t["status"] == "APPROVED" and len(t["params"]) == len(fields)),
                "preview": _fill_template(t, fields, sample)[1] if t else None}
    return {
        "key": r["event_key"], "label": label, "kind": kind, "enabled": bool(r["enabled"]),
        "recipient_ids": json.loads(r["recipient_ids"]), "priority": r["priority"],
        "preferences": {**_default_prefs(kind), **json.loads(r["preferences"] or "{}")},
        "template": template, "default_template": default_template, "customized": r["template"] is not None,
        "fields": sorted(sample), "preview": render(template, sample), "meta_template": meta,
    }


def list_rules() -> list[dict]:
    with _conn() as conn:
        rows = {r["event_key"]: r for r in conn.execute("SELECT * FROM wa_rules WHERE org_id = ?", (org_id(),))}
    return [_rule(rows[k]) for k in EVENTS if k in rows]


def get_rule(key: str) -> dict | None:
    return next((r for r in list_rules() if r["key"] == key), None)


def update_rule(key: str, enabled=None, recipient_ids=None, priority=None, preferences=None, template=None,
                reset_template: bool = False, meta_template: dict | None = None, clear_meta_template: bool = False) -> dict:
    if key not in EVENTS:
        raise KeyError(key)
    current = get_rule(key)
    sets, vals = [], []
    if enabled is not None:
        sets.append("enabled = ?"), vals.append(int(bool(enabled)))
    if recipient_ids is not None:
        known = {r["id"] for r in list_recipients()}
        if any(i not in known for i in recipient_ids):
            raise ValueError("Unknown recipient")
        sets.append("recipient_ids = ?"), vals.append(json.dumps(sorted(set(recipient_ids))))
    if priority is not None:
        if priority not in PRIORITIES:
            raise ValueError(f"Priority must be one of: {', '.join(PRIORITIES)}")
        sets.append("priority = ?"), vals.append(priority)
    if preferences is not None:
        prefs = {**current["preferences"], **preferences}
        for k in ("quiet_from", "quiet_to", "send_at"):
            if k in prefs and not _hhmm_ok(prefs[k] or ""):
                raise ValueError("Times must look like 22:00")
        if bool(prefs.get("quiet_from")) != bool(prefs.get("quiet_to")):
            raise ValueError("Quiet hours need both a start and an end")
        cd = prefs.get("cooldown_minutes", 0)
        if not isinstance(cd, (int, float)) or not 0 <= cd <= 1440:
            raise ValueError("The cooldown must be between 0 and 1440 minutes")
        if current["kind"] == "report" and not prefs.get("send_at"):
            raise ValueError("A report needs a send time")
        sets.append("preferences = ?"), vals.append(json.dumps(prefs))
    if reset_template:
        sets.append("template = NULL")
    elif template is not None:
        t = template.strip()
        if not t:
            raise ValueError("The message can't be empty")
        if len(t) > 1000:
            raise ValueError("Keep the message under 1000 characters")
        sets.append("template = ?"), vals.append(t)
    if clear_meta_template:
        sets += ["meta_template_name = NULL", "meta_template_language = NULL", "meta_params = NULL"]
    elif meta_template is not None:
        t = _get_template(meta_template.get("name") or "", meta_template.get("language") or "")
        if t is None:
            raise ValueError("That template is not in your WhatsApp Business Account (sync templates first)")
        if t["status"] != "APPROVED":
            raise ValueError(f"Meta has not approved that template (status {t['status']})")
        fields = list(meta_template.get("params") or [])
        if len(fields) != len(t["params"]):
            raise ValueError(f"The template has {len(t['params'])} variable(s); choose an SMC field for each")
        if any(f not in EVENTS[key][4] for f in fields):
            raise ValueError("Unknown field for this notification")
        sets += ["meta_template_name = ?", "meta_template_language = ?", "meta_params = ?"]
        vals += [t["name"], t["language"], json.dumps(fields)]
    if current["enabled"] is False and enabled and not (recipient_ids if recipient_ids is not None else current["recipient_ids"]):
        raise ValueError("Choose at least one recipient before switching this rule on")
    if sets:
        with _conn() as conn:
            conn.execute(f"UPDATE wa_rules SET {', '.join(sets)}, updated_at = ? WHERE org_id = ? AND event_key = ?",
                         (*vals, time.time(), org_id(), key))
    return get_rule(key)


# ---- queue and sending ----------------------------------------------------------------------------

def _compose(rule: dict, recipient: dict, values: dict, text: str, now: float) -> tuple[str | None, dict | None, str]:
    """(message type, Meta payload, text shown in the history) or (None, None, reason it can't go)."""
    meta = rule.get("meta_template")
    if meta and meta["usable"]:
        t = _get_template(meta["name"], meta["language"])
        obj, shown = _fill_template(t, meta["params"], values)
        return "template", {"type": "template", "template": obj}, shown
    if recipient.get("last_inbound_at") and now - recipient["last_inbound_at"] < WINDOW_SECONDS:
        return "text", {"type": "text", "text": {"preview_url": False, "body": text}}, text
    why = ("its linked template is not approved" if meta else "no approved WhatsApp template is linked to it")
    return None, None, (f"{rule['label']}: {why}, and {recipient['name']} has not messaged your WhatsApp number in the last 24 hours, "
                        "so Meta would refuse a free-form message")


def _insert(org: str, event_key: str, recipient: dict, body: str, status: str, *, error=None, is_test=False, source_ref=None,
            message_type=None, payload=None, now: float) -> dict | None:
    """Logs one message. Returns None when it was already logged for this event, source and
    recipient (de-duplication)."""
    with _conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO wa_messages (org_id, ts, event_key, recipient_id, recipient_name, phone, body, status, error, is_test, "
            "source_ref, status_at, message_type, payload, attempts, next_attempt_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (org, now, event_key, recipient.get("id"), recipient.get("name"), recipient["phone"], body, status, error, int(is_test),
             source_ref, now, message_type, json.dumps(payload) if payload else None, now if status == "queued" else None))
        if not cur.rowcount:
            return None
        row = conn.execute("SELECT * FROM wa_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _message(row)


def enqueue(rule: dict, recipient: dict, values: dict, text: str, *, is_test=False, source_ref=None, now: float | None = None) -> dict | None:
    """Queues one message for the sender, or logs why it can't be sent. Never calls Meta."""
    now = now if now is not None else time.time()
    org = org_id()
    kw = {"is_test": is_test, "source_ref": source_ref, "now": now}
    if not is_connected():
        return _insert(org, rule["key"], recipient, text, "not_sent", error="WhatsApp is not connected", **kw)
    if recipient["opt_in"]["status"] != "opted_in":
        why = "opted out" if recipient["opt_in"]["status"] == "opted_out" else "not opted in"
        return _insert(org, rule["key"], recipient, text, "not_sent", error=f"{recipient['name']} has {why} to WhatsApp notifications", **kw)
    mtype, payload, shown = _compose(rule, recipient, values, text, now)
    if mtype is None:
        return _insert(org, rule["key"], recipient, text, "not_sent", error=shown, **kw)
    return _insert(org, rule["key"], recipient, shown, "queued", message_type=mtype, payload=payload, **kw)


def _claim(mid: int) -> bool:
    with _conn() as conn:
        return conn.execute("UPDATE wa_messages SET status = 'sending', status_at = ? WHERE id = ? AND status IN ('queued', 'retrying')",
                            (time.time(), mid)).rowcount == 1


def _finish(mid: int, status: str, now: float, *, wamid=None, error=None, attempts=None, next_at=None) -> None:
    with _conn() as conn:
        conn.execute("UPDATE wa_messages SET status = ?, wamid = COALESCE(?, wamid), error = ?, status_at = ?, "
                     "attempts = COALESCE(?, attempts), next_attempt_at = ? WHERE id = ?",
                     (status, wamid, error, now, attempts, next_at, mid))


def deliver(mid: int, now: float | None = None) -> dict | None:
    """Sends one queued message to Meta and records the real outcome (retrying on temporary errors)."""
    now = now if now is not None else time.time()
    if not _claim(mid):
        return None
    with _conn() as conn:
        row = conn.execute("SELECT * FROM wa_messages WHERE id = ?", (mid,)).fetchone()
    org = row["org_id"]
    c = _row(org)
    attempts = (row["attempts"] or 0) + 1
    if c.get("status") != "connected":
        _finish(mid, "not_sent", now, error="WhatsApp is not connected", attempts=attempts)
    else:
        try:
            cred = _credentials(c)
            res = graph.call("POST", f"{cred['phone_number_id']}/messages", cred["token"],
                             {"messaging_product": "whatsapp", "recipient_type": "individual", "to": row["phone"], **json.loads(row["payload"])})
            _finish(mid, "sent", now, wamid=res["messages"][0]["id"], attempts=attempts)
            log.info("smc-connect: %s message %s accepted by Meta (to %s)", row["event_key"], mid, mask(row["phone"]))
        except MetaError as e:
            if e.transient and attempts < MAX_ATTEMPTS:
                wait = RETRY_BACKOFF_SECONDS[min(attempts - 1, len(RETRY_BACKOFF_SECONDS) - 1)]
                _finish(mid, "retrying", now, error=f"{e} (attempt {attempts} of {MAX_ATTEMPTS}; retrying)", attempts=attempts, next_at=now + wait)
            else:
                _finish(mid, "failed", now, error=str(e), attempts=attempts)
            if e.code == TOKEN_REJECTED or e.http_status == 401:
                _fail(org, f"Meta rejected the access token: {e}")
            log.warning("smc-connect: message %s: %s", mid, e)
        except (NotConfigured, credential_store.Undecryptable, credential_store.NotConfigured) as e:
            _finish(mid, "failed", now, error=str(e), attempts=attempts)
        except (KeyError, IndexError, TypeError):
            _finish(mid, "failed", now, error="Meta's answer had no message id", attempts=attempts)
    with _conn() as conn:
        return _message(conn.execute("SELECT * FROM wa_messages WHERE id = ?", (mid,)).fetchone())


def process_queue(now: float | None = None, limit: int = 50) -> int:
    """Sends every message that is due (all organizations: each with its own credentials)."""
    now = now if now is not None else time.time()
    with _conn() as conn:
        conn.execute("UPDATE wa_messages SET status = 'retrying', next_attempt_at = ? WHERE status = 'sending' AND status_at < ?",
                     (now, now - SENDING_STALE_SECONDS))
        ids = [r[0] for r in conn.execute("SELECT id FROM wa_messages WHERE status IN ('queued', 'retrying') AND next_attempt_at <= ? "
                                          "ORDER BY next_attempt_at, id LIMIT ?", (now, limit))]
    for mid in ids:
        deliver(mid, now)
    return len(ids)


def send_test(recipient_id: int, event_key: str = "daily_summary") -> dict:
    """One real test message to a recipient (only recipients: never an arbitrary number), sent
    right away. The answer is what actually happened."""
    rec = get_recipient(recipient_id)
    if rec is None:
        raise ValueError("Unknown recipient")
    rule = get_rule(event_key) or get_rule("daily_summary")
    sample = EVENTS[rule["key"]][4]
    text = "✅ *SMC Connect test message*\nThis is how your notifications will look:\n\n" + rule["preview"]
    m = enqueue(rule, rec, sample, text, is_test=True)
    if m and m["status"] == "queued":
        m = deliver(m["id"]) or m
    return m


def _in_quiet(prefs: dict, now: float) -> bool:
    a, b = prefs.get("quiet_from"), prefs.get("quiet_to")
    if not a or not b:
        return False
    t = datetime.datetime.fromtimestamp(now).strftime("%H:%M")
    return a <= t < b if a <= b else (t >= a or t < b)


def notify(event_key: str, values: dict, source_ref: str | None = None, now: float | None = None) -> list[dict]:
    """Queues a rule's message for its recipients, honouring enabled, quiet hours and cooldown.
    Returns the logged rows (queued or not_sent); [] when the rule held it back or it was a duplicate."""
    now = now if now is not None else time.time()
    rule = get_rule(event_key)
    if not rule or not rule["enabled"]:
        return []
    prefs = rule["preferences"]
    if rule["priority"] != "High" and _in_quiet(prefs, now):
        return []
    cd = prefs.get("cooldown_minutes") or 0
    if cd and rule["kind"] == "event":
        with _conn() as conn:
            last = conn.execute("SELECT MAX(ts) FROM wa_messages WHERE org_id = ? AND event_key = ? AND is_test = 0",
                                (org_id(), event_key)).fetchone()[0]
        if last and now - last < cd * 60:
            return []
    recipients = [r for r in list_recipients() if r["id"] in rule["recipient_ids"] and r["active"]]
    text = render(rule["template"], values)
    out = [enqueue(rule, r, values, text, source_ref=source_ref, now=now) for r in recipients]
    return [m for m in out if m]


# ---- Meta webhook ---------------------------------------------------------------------------------

def app_secret() -> str:
    return os.environ.get("META_APP_SECRET") or os.environ.get("WHATSAPP_APP_SECRET", "")


def check_signature(raw: bytes, header: str | None) -> bool:
    secret = app_secret()
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def _org_for_phone(pnid: str | None) -> str | None:
    if not pnid:
        return None
    with _conn() as conn:
        r = conn.execute("SELECT org_id FROM wa_connections WHERE phone_number_id = ?", (str(pnid),)).fetchone()
    return r["org_id"] if r else None


def _org_for_waba(waba_id: str | None) -> str | None:
    if not waba_id:
        return None
    with _conn() as conn:
        r = conn.execute("SELECT org_id FROM wa_business_accounts WHERE waba_id = ? ORDER BY updated_at DESC", (str(waba_id),)).fetchone()
    return r["org_id"] if r else None


def handle_webhook(payload: dict) -> dict:
    """Meta's callbacks, each applied only to the organization that owns the number / account:
    delivery statuses, incoming messages (open the 24-hour window; STOP / START opt out / in) and
    template status changes. Message texts from recipients are not stored."""
    out = {"statuses": 0, "messages": 0, "templates": 0, "ignored": 0}
    order = {"queued": 0, "retrying": 0, "sending": 0, "sent": 1, "delivered": 2, "read": 3}
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            if change.get("field") == "message_template_status_update":
                org = _org_for_waba(entry.get("id"))
                if not org:
                    out["ignored"] += 1
                    continue
                with _conn() as conn:
                    out["templates"] += conn.execute(
                        "UPDATE wa_templates SET status = ? WHERE org_id = ? AND name = ? AND language = ?",
                        ((value.get("event") or "").upper(), org, value.get("message_template_name"), value.get("message_template_language"))).rowcount
                continue
            org = _org_for_phone((value.get("metadata") or {}).get("phone_number_id"))
            if not org:
                out["ignored"] += 1
                continue
            for st in value.get("statuses", []) or []:
                wamid, status = st.get("id"), st.get("status")
                if not wamid or status not in ("sent", "delivered", "read", "failed"):
                    continue
                err = None
                if status == "failed":
                    errs = st.get("errors") or [{}]
                    err = errs[0].get("title") or errs[0].get("message") or "Delivery failed"
                with _conn() as conn:
                    row = conn.execute("SELECT id, status FROM wa_messages WHERE wamid = ? AND org_id = ?", (wamid, org)).fetchone()
                    if row is None or (status != "failed" and order.get(row["status"], 0) >= order[status]):
                        continue                       # unknown, or would go backwards (read -> delivered)
                    conn.execute("UPDATE wa_messages SET status = ?, error = ?, status_at = ? WHERE id = ?",
                                 (status, err, float(st.get("timestamp") or time.time()), row["id"]))
                    out["statuses"] += 1
            for msg in value.get("messages", []) or []:
                sender = re.sub(r"\D", "", str(msg.get("from") or ""))
                text = ((msg.get("text") or {}).get("body") or (msg.get("button") or {}).get("text") or "").strip().upper()
                ts = float(msg.get("timestamp") or time.time())
                with _conn() as conn:
                    r = conn.execute("SELECT id FROM wa_recipients WHERE org_id = ? AND phone = ?", (org, sender)).fetchone()
                    if r is None:
                        continue                       # not a recipient: nothing to record
                    conn.execute("UPDATE wa_recipients SET last_inbound_at = MAX(COALESCE(last_inbound_at, 0), ?) WHERE id = ?", (ts, r["id"]))
                    if text in OPT_OUT_WORDS:
                        conn.execute("UPDATE wa_recipients SET opt_in_status = 'opted_out', opted_out_at = ?, opt_in_by = 'WhatsApp reply' WHERE id = ?",
                                     (ts, r["id"]))
                    elif text in OPT_IN_WORDS:
                        conn.execute("UPDATE wa_recipients SET opt_in_status = 'opted_in', opt_in_method = 'whatsapp_reply', opt_in_at = ?, "
                                     "opt_in_by = 'WhatsApp reply', opted_out_at = NULL WHERE id = ?", (ts, r["id"]))
                    out["messages"] += 1
    return out


# ---- history and stats ----------------------------------------------------------------------------

def _message(r) -> dict:
    label = EVENTS.get(r["event_key"], (r["event_key"],))[0]
    status = "queued" if r["status"] == "sending" else r["status"]
    return {"id": r["id"], "ts": r["ts"], "event_key": r["event_key"], "event_label": label, "recipient_id": r["recipient_id"],
            "recipient_name": r["recipient_name"], "phone_masked": mask(r["phone"]), "body": r["body"], "status": status,
            "error": r["error"], "is_test": bool(r["is_test"]), "status_at": r["status_at"], "has_meta_id": bool(r["wamid"]),
            "message_type": r["message_type"], "attempts": r["attempts"] or 0, "next_attempt_at": r["next_attempt_at"]}


def history(q: str = "", event_key: str | None = None, status: str | None = None, start_ts: float | None = None,
            end_ts: float | None = None, limit: int = 500) -> list[dict]:
    sql, params = "SELECT * FROM wa_messages WHERE org_id = ?", [org_id()]
    if event_key:
        sql, params = sql + " AND event_key = ?", params + [event_key]
    if status:
        sql, params = sql + " AND status IN (?, ?)", params + [status, "sending" if status == "queued" else status]
    if start_ts is not None:
        sql, params = sql + " AND ts >= ?", params + [start_ts]
    if end_ts is not None:
        sql, params = sql + " AND ts <= ?", params + [end_ts]
    with _conn() as conn:
        rows = [_message(r) for r in conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT ?", (*params, limit))]
    needle = (q or "").strip().lower()
    if needle:
        rows = [m for m in rows if needle in f"{m['recipient_name']} {m['body']} {m['event_label']} {m['phone_masked']}".lower()]
    return rows


def stats() -> dict:
    org = org_id()
    with _conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) FROM wa_messages WHERE org_id = ? AND is_test = 0 GROUP BY status", (org,)).fetchall()
        last = conn.execute("SELECT * FROM wa_messages WHERE org_id = ? AND status IN ('sent', 'delivered', 'read') ORDER BY ts DESC LIMIT 1",
                            (org,)).fetchone()
    c = {r[0]: r[1] for r in rows}
    return {
        "sent": c.get("sent", 0) + c.get("delivered", 0) + c.get("read", 0),
        "delivered": c.get("delivered", 0) + c.get("read", 0),
        "read": c.get("read", 0),
        "failed": c.get("failed", 0),
        "not_sent": c.get("not_sent", 0),
        "pending": c.get("queued", 0) + c.get("retrying", 0) + c.get("sending", 0),
        "last_success": _message(last) if last else None,
    }


# ---- dispatcher (reads alerts, visitor check-ins, daily reports) and sender ------------------------

def _fmt(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%d %b %Y, %I:%M %p")


class Dispatcher:
    """Two background threads, neither of which camera or AI code ever waits on: one turns new
    SMC events into queued messages, the other sends the queue to Meta."""

    def __init__(self):
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._prime()
        threading.Thread(target=self._loop, args=(self.run_once, POLL_SECONDS), name="smc-connect", daemon=True).start()
        threading.Thread(target=self._loop, args=(process_queue, SEND_POLL_SECONDS), name="smc-connect-sender", daemon=True).start()

    def _prime(self) -> None:
        """Start from now: never send a backlog of old alerts / check-ins after a (re)start."""
        with _conn() as conn:
            if _get("last_alert_id") is None:
                _set("last_alert_id", conn.execute("SELECT COALESCE(MAX(id), 0) FROM alerts").fetchone()[0] if _table(conn, "alerts") else 0)
            if _get("last_visit_entry") is None:
                _set("last_visit_entry", time.time())

    @staticmethod
    def _loop(fn, every: float) -> None:
        while True:
            try:
                fn()
            except Exception:
                log.exception("smc-connect: background pass failed")
            if lifecycle.wait(every):
                return

    def run_once(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self._alerts()
        self._visitors()
        self._reports(now)

    def _alerts(self) -> None:
        from app import camera_db

        last = _get("last_alert_id", 0) or 0
        with _conn() as conn:
            if not _table(conn, "alerts"):
                return
            rows = conn.execute("SELECT * FROM alerts WHERE id > ? ORDER BY id LIMIT 200", (last,)).fetchall()
        if not rows:
            return
        cams = {c["id"]: c["name"] for c in camera_db.list_cameras()}
        zones = {}
        try:
            from app import intrusion
            zones = {z["id"]: z for z in intrusion.list_zones()}
        except Exception:
            pass
        for r in rows:
            key = ALERT_EVENT_RULE.get(r["event"])
            if key:
                zone = zones.get(r["zone_id"]) if "zone_id" in r.keys() else None
                notify(key, {"camera": cams.get(r["camera_id"], f"Camera {r['camera_id']}"), "time": _fmt(r["ts"]),
                             "message": r["message"] or "", "zone": (r["zone_name"] if "zone_name" in r.keys() else None) or (zone or {}).get("name") or "-",
                             "area": (zone or {}).get("area") or "-", "link": "SMC > Analytics > Intrusion"},
                       source_ref=f"alert:{r['id']}", now=r["ts"])
        _set("last_alert_id", rows[-1]["id"])

    def _visitors(self) -> None:
        last = _get("last_visit_entry", time.time())
        with _conn() as conn:
            if not _table(conn, "vm_visits"):
                return
            rows = conn.execute("SELECT * FROM vm_visits WHERE entry_at IS NOT NULL AND entry_at > ? ORDER BY entry_at LIMIT 100", (last,)).fetchall()
        for r in rows:
            notify("visitor", {"visitor": r["visitor_name"], "company": r["visitor_company"] or "-", "host": r["host_name"],
                               "purpose": r["purpose"], "time": _fmt(r["entry_at"])}, source_ref=f"visit:{r['id']}", now=r["entry_at"])
        if rows:
            _set("last_visit_entry", rows[-1]["entry_at"])

    def _reports(self, now: float) -> None:
        today = datetime.date.fromtimestamp(now).isoformat()
        hhmm = datetime.datetime.fromtimestamp(now).strftime("%H:%M")
        sent = _get("reports_sent", {}) or {}
        for rule in list_rules():
            if rule["kind"] != "report" or not rule["enabled"]:
                continue
            at = rule["preferences"].get("send_at") or "20:00"
            if hhmm < at or sent.get(rule["key"]) == today:
                continue
            sent[rule["key"]] = today
            _set("reports_sent", sent)       # once a day; the source_ref also stops a repeat after a restart
            values = report_values(rule["key"], today)
            if values is not None:
                notify(rule["key"], values, source_ref=f"report:{rule['key']}:{today}", now=now)


def report_values(key: str, day: str) -> dict | None:
    """The dashboard's own numbers for a daily report (insights.business_overview)."""
    from app import insights

    b = insights.business_overview(day)
    date = datetime.date.fromisoformat(day).strftime("%d %b %Y")
    if key == "attendance":
        w = b.get("workforce")
        return None if not w else {"date": date, "present": w["present"], "total": w["total"], "attendance_pct": w["attendance_pct"],
                                   "late": w["late"], "absent": w["absent"]}
    if key == "footfall":
        f = b.get("footfall")
        if not f:
            return None
        peak = f.get("peak_hour")
        return {"date": date, "entries": f["entries"], "employee_entries": f.get("employee_entries", 0),
                "visitor_entries": f.get("visitor_entries", 0),
                "peak_hour": "-" if peak is None else datetime.time(peak).strftime("%I %p").lstrip("0")}
    if key == "daily_summary":
        return {"date": date, "summary": "\n".join(f"• {s['area']}: {s['text']}" for s in b.get("summary", [])) or "No activity recorded."}
    return None


service = Dispatcher()
