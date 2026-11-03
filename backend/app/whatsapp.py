"""
whatsapp.py

SMC Connect: WhatsApp Business notifications through the Meta WhatsApp Cloud API.

Credentials (environment only, never stored in the database or sent to the browser):
  WHATSAPP_ACCESS_TOKEN          system-user access token of the Meta app
  WHATSAPP_PHONE_NUMBER_ID       the business phone number's id in WhatsApp Manager
  WHATSAPP_BUSINESS_ACCOUNT_ID   the WhatsApp Business Account (WABA) id
  WHATSAPP_APP_SECRET            to verify the signature of delivery-status webhooks
  WHATSAPP_WEBHOOK_VERIFY_TOKEN  the token Meta echoes when the webhook is registered
  WHATSAPP_API_VERSION           Graph API version (default v21.0)

Connection is never assumed. "connected" is stored only after Meta answered for the configured
phone number (and it matches the number the operator entered); without credentials the page says
Not configured / Demo mode, and nothing is reported as sent or delivered.

What gets sent, and when (the dispatcher only READS the other modules; nothing in cameras, AI,
attendance or visitors changes):
  intrusion / loitering / late_arrival   new rows in the alerts table (alerts.py)
  visitor                               new visitor check-ins (vm_visits.entry_at)
  attendance / footfall / daily_summary  a daily report at each rule's send time, from
                                         insights.business_overview (the dashboard's numbers)
Each rule has recipients, a priority, quiet hours (Low / Normal wait them out, High always goes)
and a cooldown. Every attempt is logged with its real outcome: sent (accepted by Meta), delivered
/ read (Meta's status webhook), failed (with Meta's error) or not_sent (WhatsApp not connected).
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
import urllib.request

from app import config, db, lifecycle

log = logging.getLogger("whatsapp")

DB_PATH = config.DB_PATH
POLL_SECONDS = 15
GRAPH = "https://graph.facebook.com"

ROLES = ("Admin", "Security Manager", "HR Manager", "Reception")
PRIORITIES = ("High", "Normal", "Low")
STATUSES = ("sent", "delivered", "read", "failed", "not_sent")

# The notification rules: key -> (label, kind, default priority, default template, sample values).
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


class NotConfigured(Exception):
    """No Meta credentials in the environment."""


class MetaError(Exception):
    """Meta answered with an error (message is Meta's)."""


# ---- Meta WhatsApp Cloud API client ---------------------------------------------------

class MetaCloudClient:
    """The few Cloud API calls SMC needs. Every network call goes through _call, so tests replace it."""

    def __init__(self):
        self.token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
        self.phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
        self.waba_id = os.environ.get("WHATSAPP_BUSINESS_ACCOUNT_ID", "")
        self.version = os.environ.get("WHATSAPP_API_VERSION", "v21.0")

    @property
    def configured(self) -> bool:
        return bool(self.token and self.phone_number_id)

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.configured:
            raise NotConfigured("WhatsApp Cloud API credentials are not set on the server")
        req = urllib.request.Request(
            f"{GRAPH}/{self.version}/{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as res:  # noqa: S310 - fixed Graph host
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode()).get("error", {})
                raise MetaError(err.get("message") or f"Meta answered {e.code}") from e
            except ValueError:
                raise MetaError(f"Meta answered {e.code}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise MetaError(f"Could not reach Meta: {e}") from e

    def phone_info(self) -> dict:
        return self._call("GET", f"{self.phone_number_id}?fields=display_phone_number,verified_name,quality_rating,code_verification_status")

    def account_info(self) -> dict | None:
        if not self.waba_id:
            return None
        return self._call("GET", f"{self.waba_id}?fields=name,account_review_status")

    def send_text(self, to: str, body: str) -> str:
        """Returns Meta's message id. Free-form text is delivered inside the 24 h customer-service
        window; for business-initiated messages outside it Meta requires an approved template
        (send_template)."""
        r = self._call("POST", f"{self.phone_number_id}/messages",
                       {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"preview_url": False, "body": body}})
        return r["messages"][0]["id"]

    def send_template(self, to: str, name: str, language: str, parameters: list[str]) -> str:
        r = self._call("POST", f"{self.phone_number_id}/messages", {
            "messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {"name": name, "language": {"code": language},
                         "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in parameters]}]},
        })
        return r["messages"][0]["id"]


client = MetaCloudClient()


# ---- storage ------------------------------------------------------------------------------

def _conn():
    return db.connect(DB_PATH, row_factory=sqlite3.Row)


def init_db() -> None:
    with _conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS wa_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_recipients (
                           id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE,
                           role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_rules (
                           event_key TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, recipient_ids TEXT NOT NULL DEFAULT '[]',
                           priority TEXT NOT NULL, preferences TEXT NOT NULL DEFAULT '{}', template TEXT, updated_at REAL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS wa_messages (
                           id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, event_key TEXT NOT NULL,
                           recipient_id INTEGER, recipient_name TEXT, phone TEXT NOT NULL, body TEXT NOT NULL,
                           status TEXT NOT NULL, wamid TEXT, error TEXT, is_test INTEGER NOT NULL DEFAULT 0,
                           source_ref TEXT, status_at REAL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_messages_ts ON wa_messages (ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_wa_messages_wamid ON wa_messages (wamid)")
        for key, (label, kind, priority, template, _s) in EVENTS.items():
            conn.execute("INSERT OR IGNORE INTO wa_rules (event_key, enabled, priority, preferences, template, updated_at) "
                         "VALUES (?, 0, ?, ?, NULL, ?)", (key, priority, json.dumps(_default_prefs(kind)), time.time()))


def _default_prefs(kind: str) -> dict:
    prefs = {"quiet_from": "", "quiet_to": "", "cooldown_minutes": 0 if kind == "report" else 5}
    if kind == "report":
        prefs["send_at"] = "20:00"
    return prefs


def _get(key: str, default=None):
    with _conn() as conn:
        row = conn.execute("SELECT value FROM wa_settings WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def _set(key: str, value) -> None:
    with _conn() as conn:
        conn.execute("INSERT INTO wa_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (key, json.dumps(value)))


# ---- phone numbers ------------------------------------------------------------------------

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


# ---- connection ---------------------------------------------------------------------------

def connection() -> dict:
    """What the page shows. Never says connected unless Meta confirmed it."""
    c = _get("connection", {}) or {}
    status = c.get("status") or "not_connected"
    if not client.configured:
        status = "not_configured"
    return {
        "status": status,                     # not_configured | not_connected | pending | connected | failed | disconnected
        "configured": client.configured,
        "demo_mode": not client.configured,
        "entered_number": c.get("entered_number"),
        "business_number": c.get("display_phone_number") if status == "connected" else None,
        "verified_name": c.get("verified_name") if status == "connected" else None,
        "quality_rating": c.get("quality_rating") if status == "connected" else None,
        "business_account": c.get("business_account") if status == "connected" else None,
        "checked_at": c.get("checked_at"),
        "connected_at": c.get("connected_at") if status == "connected" else None,
        "last_error": c.get("last_error"),
    }


def start_connect(country_code: str, number: str) -> dict:
    """Step 1 of the wizard: the number the business wants to connect. Not connected yet."""
    phone = normalize_phone(country_code, number)
    c = _get("connection", {}) or {}
    c.update({"status": "pending", "entered_number": phone, "last_error": None, "checked_at": time.time()})
    _set("connection", c)
    return connection()


def verify() -> dict:
    """Asks Meta about the configured phone number (and business account). Connected only when
    Meta answered and its number is the one entered in the wizard."""
    c = _get("connection", {}) or {}
    c["checked_at"] = time.time()
    if not client.configured:
        c.update({"status": "not_connected", "last_error": "WhatsApp Cloud API credentials are not set on the server (Demo mode)."})
        _set("connection", c)
        return connection()
    try:
        info = client.phone_info()
        account = None
        try:
            account = client.account_info()
        except MetaError as e:
            account = {"error": str(e)}
    except (MetaError, NotConfigured) as e:
        c.update({"status": "failed", "last_error": str(e)})
        _set("connection", c)
        return connection()
    meta_digits = re.sub(r"\D", "", info.get("display_phone_number") or "")
    entered = c.get("entered_number")
    if entered and meta_digits and meta_digits != entered:
        c.update({"status": "failed", "last_error": f"Meta's number for this account is {mask(meta_digits)}, not {mask(entered)}."})
        _set("connection", c)
        return connection()
    c.update({
        "status": "connected", "last_error": None, "display_phone_number": info.get("display_phone_number"),
        "verified_name": info.get("verified_name"), "quality_rating": info.get("quality_rating"),
        "business_account": ({"name": account.get("name"), "review_status": account.get("account_review_status"),
                              "error": account.get("error")} if account else None),
        "connected_at": c.get("connected_at") if c.get("status") == "connected" else time.time(),
        "entered_number": entered or meta_digits,
    })
    _set("connection", c)
    return connection()


def disconnect() -> dict:
    c = _get("connection", {}) or {}
    c.update({"status": "disconnected", "last_error": None, "checked_at": time.time()})
    _set("connection", c)
    return connection()


def is_connected() -> bool:
    return connection()["status"] == "connected"


# ---- recipients ------------------------------------------------------------------------------

def _recipient(r) -> dict:
    return {"id": r["id"], "name": r["name"], "phone": r["phone"], "phone_masked": mask(r["phone"]), "role": r["role"],
            "active": bool(r["active"]), "created_at": r["created_at"]}


def list_recipients() -> list[dict]:
    with _conn() as conn:
        return [_recipient(r) for r in conn.execute("SELECT * FROM wa_recipients ORDER BY name")]


def add_recipient(name: str, country_code: str, number: str, role: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("Enter the recipient's name")
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
    phone = normalize_phone(country_code, number)
    try:
        with _conn() as conn:
            cur = conn.execute("INSERT INTO wa_recipients (name, phone, role, active, created_at) VALUES (?, ?, ?, 1, ?)",
                               (name[:80], phone, role, time.time()))
            row = conn.execute("SELECT * FROM wa_recipients WHERE id = ?", (cur.lastrowid,)).fetchone()
    except sqlite3.IntegrityError as e:
        raise ValueError("This phone number is already a recipient") from e
    return _recipient(row)


def update_recipient(rid: int, **fields) -> dict | None:
    sets, vals = [], []
    if "name" in fields and fields["name"] is not None:
        if not fields["name"].strip():
            raise ValueError("Enter the recipient's name")
        sets.append("name = ?"), vals.append(fields["name"].strip()[:80])
    if "role" in fields and fields["role"] is not None:
        if fields["role"] not in ROLES:
            raise ValueError(f"Role must be one of: {', '.join(ROLES)}")
        sets.append("role = ?"), vals.append(fields["role"])
    if "active" in fields and fields["active"] is not None:
        sets.append("active = ?"), vals.append(int(bool(fields["active"])))
    with _conn() as conn:
        if sets:
            conn.execute(f"UPDATE wa_recipients SET {', '.join(sets)} WHERE id = ?", (*vals, rid))
        row = conn.execute("SELECT * FROM wa_recipients WHERE id = ?", (rid,)).fetchone()
    return _recipient(row) if row else None


def delete_recipient(rid: int) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM wa_recipients WHERE id = ?", (rid,))
        for r in conn.execute("SELECT event_key, recipient_ids FROM wa_rules").fetchall():
            ids = [i for i in json.loads(r["recipient_ids"]) if i != rid]
            conn.execute("UPDATE wa_rules SET recipient_ids = ? WHERE event_key = ?", (json.dumps(ids), r["event_key"]))


# ---- rules and templates -----------------------------------------------------------------------

def _hhmm_ok(v: str) -> bool:
    return v == "" or bool(re.match(r"^([01]\d|2[0-3]):[0-5]\d$", v or ""))


def render(template: str, values: dict) -> str:
    """Fills {field} placeholders; an unknown field is left as written so nothing is invented."""
    return PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values and values[m.group(1)] is not None else m.group(0), template)


def _rule(r) -> dict:
    label, kind, default_priority, default_template, sample = EVENTS[r["event_key"]]
    template = r["template"] or default_template
    return {
        "key": r["event_key"], "label": label, "kind": kind, "enabled": bool(r["enabled"]),
        "recipient_ids": json.loads(r["recipient_ids"]), "priority": r["priority"],
        "preferences": {**_default_prefs(kind), **json.loads(r["preferences"] or "{}")},
        "template": template, "default_template": default_template, "customized": r["template"] is not None,
        "fields": sorted(sample), "preview": render(template, sample),
    }


def list_rules() -> list[dict]:
    with _conn() as conn:
        rows = {r["event_key"]: r for r in conn.execute("SELECT * FROM wa_rules")}
    return [_rule(rows[k]) for k in EVENTS if k in rows]


def get_rule(key: str) -> dict | None:
    return next((r for r in list_rules() if r["key"] == key), None)


def update_rule(key: str, enabled=None, recipient_ids=None, priority=None, preferences=None, template=None,
                reset_template: bool = False) -> dict:
    if key not in EVENTS:
        raise KeyError(key)
    current = get_rule(key)
    sets, vals = [], []
    if enabled is not None:
        sets.append("enabled = ?"), vals.append(int(bool(enabled)))
    if recipient_ids is not None:
        known = {r["id"] for r in list_recipients()}
        bad = [i for i in recipient_ids if i not in known]
        if bad:
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
    if current["enabled"] is False and enabled and not (recipient_ids if recipient_ids is not None else current["recipient_ids"]):
        raise ValueError("Choose at least one recipient before switching this rule on")
    if sets:
        with _conn() as conn:
            conn.execute(f"UPDATE wa_rules SET {', '.join(sets)}, updated_at = ? WHERE event_key = ?", (*vals, time.time(), key))
    return get_rule(key)


# ---- sending ---------------------------------------------------------------------------------

def _log(event_key, recipient, body, status, wamid=None, error=None, is_test=False, source_ref=None, now=None) -> dict:
    now = now if now is not None else time.time()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO wa_messages (ts, event_key, recipient_id, recipient_name, phone, body, status, wamid, error, is_test, source_ref, status_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (now, event_key, recipient.get("id"), recipient.get("name"), recipient["phone"], body, status, wamid, error,
             int(is_test), source_ref, now),
        )
        row = conn.execute("SELECT * FROM wa_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _message(row)


def send(event_key: str, recipient: dict, body: str, is_test: bool = False, source_ref: str | None = None) -> dict:
    """One message to one recipient, logged with its real outcome."""
    if not is_connected():
        return _log(event_key, recipient, body, "not_sent", error="WhatsApp is not connected", is_test=is_test, source_ref=source_ref)
    try:
        wamid = client.send_text(recipient["phone"], body)
    except (MetaError, NotConfigured) as e:
        return _log(event_key, recipient, body, "failed", error=str(e), is_test=is_test, source_ref=source_ref)
    return _log(event_key, recipient, body, "sent", wamid=wamid, is_test=is_test, source_ref=source_ref)


def send_test(recipient_id: int | None = None, country_code: str | None = None, number: str | None = None,
              event_key: str = "daily_summary") -> dict:
    if recipient_id is not None:
        rec = next((r for r in list_recipients() if r["id"] == recipient_id), None)
        if rec is None:
            raise ValueError("Unknown recipient")
    else:
        rec = {"id": None, "name": "Test number", "phone": normalize_phone(country_code, number)}
    rule = get_rule(event_key) or get_rule("daily_summary")
    body = "✅ *SMC Connect test message*\nThis is how your notifications will look:\n\n" + rule["preview"]
    return send(rule["key"], rec, body, is_test=True)


def _in_quiet(prefs: dict, now: float) -> bool:
    a, b = prefs.get("quiet_from"), prefs.get("quiet_to")
    if not a or not b:
        return False
    t = datetime.datetime.fromtimestamp(now).strftime("%H:%M")
    return a <= t < b if a <= b else (t >= a or t < b)


def notify(event_key: str, values: dict, source_ref: str | None = None, now: float | None = None) -> list[dict]:
    """Sends a rule's message to its recipients, honouring enabled, quiet hours and cooldown.
    Returns the logged messages ([] when the rule held it back)."""
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
            last = conn.execute("SELECT MAX(ts) FROM wa_messages WHERE event_key = ? AND is_test = 0", (event_key,)).fetchone()[0]
        if last and now - last < cd * 60:
            return []
    recipients = [r for r in list_recipients() if r["id"] in rule["recipient_ids"] and r["active"]]
    body = render(rule["template"], values)
    return [send(event_key, r, body, source_ref=source_ref) for r in recipients]


# ---- delivery status (Meta webhook) ---------------------------------------------------------

def check_signature(raw: bytes, header: str | None) -> bool:
    secret = os.environ.get("WHATSAPP_APP_SECRET", "")
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def apply_statuses(payload: dict) -> int:
    """Updates logged messages from Meta's status callbacks (sent / delivered / read / failed)."""
    order = {"sent": 1, "delivered": 2, "read": 3}
    updated = 0
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for st in (change.get("value") or {}).get("statuses", []) or []:
                wamid, status = st.get("id"), st.get("status")
                if not wamid or status not in ("sent", "delivered", "read", "failed"):
                    continue
                err = None
                if status == "failed":
                    errs = st.get("errors") or [{}]
                    err = errs[0].get("title") or errs[0].get("message") or "Delivery failed"
                with _conn() as conn:
                    row = conn.execute("SELECT id, status FROM wa_messages WHERE wamid = ?", (wamid,)).fetchone()
                    if row is None:
                        continue
                    if status != "failed" and order.get(row["status"], 0) >= order[status]:
                        continue                       # never go backwards (read -> delivered)
                    conn.execute("UPDATE wa_messages SET status = ?, error = ?, status_at = ? WHERE id = ?",
                                 (status, err, float(st.get("timestamp") or time.time()), row["id"]))
                    updated += 1
    return updated


# ---- history and stats ------------------------------------------------------------------------

def _message(r) -> dict:
    label = EVENTS.get(r["event_key"], (r["event_key"],))[0]
    return {"id": r["id"], "ts": r["ts"], "event_key": r["event_key"], "event_label": label, "recipient_id": r["recipient_id"],
            "recipient_name": r["recipient_name"], "phone_masked": mask(r["phone"]), "body": r["body"], "status": r["status"],
            "error": r["error"], "is_test": bool(r["is_test"]), "status_at": r["status_at"], "has_meta_id": bool(r["wamid"])}


def history(q: str = "", event_key: str | None = None, status: str | None = None, start_ts: float | None = None,
            end_ts: float | None = None, limit: int = 500) -> list[dict]:
    sql, params = "SELECT * FROM wa_messages WHERE 1 = 1", []
    if event_key:
        sql, params = sql + " AND event_key = ?", params + [event_key]
    if status:
        sql, params = sql + " AND status = ?", params + [status]
    if start_ts is not None:
        sql, params = sql + " AND ts >= ?", params + [start_ts]
    if end_ts is not None:
        sql, params = sql + " AND ts <= ?", params + [end_ts]
    with _conn() as conn:
        rows = [_message(r) for r in conn.execute(sql + " ORDER BY ts DESC LIMIT ?", (*params, limit))]
    needle = (q or "").strip().lower()
    if needle:
        rows = [m for m in rows if needle in f"{m['recipient_name']} {m['body']} {m['event_label']} {m['phone_masked']}".lower()]
    return rows


def stats() -> dict:
    with _conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) FROM wa_messages WHERE is_test = 0 GROUP BY status").fetchall()
        last = conn.execute("SELECT * FROM wa_messages WHERE status IN ('sent', 'delivered', 'read') ORDER BY ts DESC LIMIT 1").fetchone()
    c = {r[0]: r[1] for r in rows}
    return {
        "sent": c.get("sent", 0) + c.get("delivered", 0) + c.get("read", 0),
        "delivered": c.get("delivered", 0) + c.get("read", 0),
        "read": c.get("read", 0),
        "failed": c.get("failed", 0),
        "not_sent": c.get("not_sent", 0),
        "last_success": _message(last) if last else None,
    }


# ---- dispatcher (reads alerts, visitor check-ins, daily reports) -----------------------------

def _fmt(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%d %b %Y, %I:%M %p")


class Dispatcher:
    def __init__(self):
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._prime()
        threading.Thread(target=self._loop, name="smc-connect", daemon=True).start()

    def _prime(self) -> None:
        """Start from now: never send a backlog of old alerts / check-ins after a (re)start."""
        with _conn() as conn:
            if _get("last_alert_id") is None:
                _set("last_alert_id", conn.execute("SELECT COALESCE(MAX(id), 0) FROM alerts").fetchone()[0] if _table(conn, "alerts") else 0)
            if _get("last_visit_entry") is None:
                _set("last_visit_entry", time.time())

    def _loop(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception:
                log.exception("smc-connect: dispatch pass failed")
            if lifecycle.wait(POLL_SECONDS):
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
            _set("reports_sent", sent)       # once a day, even if the send fails (no retry storm)
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


def _table(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


service = Dispatcher()
