"""SMC Connect (whatsapp.py, whatsapp_routes.py) against a fake Meta Graph API."""

import hashlib
import hmac
import json
import os
import sqlite3
import time

import pytest
from cryptography.fernet import Fernet

from app import alerts, camera_db, credential_store, whatsapp
from tests.conftest import bearer

ENV_KEYS = ("WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_BUSINESS_ACCOUNT_ID", "WHATSAPP_APP_SECRET",
            "WHATSAPP_WEBHOOK_VERIFY_TOKEN", "META_APP_ID", "META_APP_SECRET", "META_EMBEDDED_SIGNUP_CONFIG_ID",
            "SMC_CREDENTIALS_KEY", "SMC_ORG_ID")

APPROVED = {"name": "smc_intrusion_alert", "language": "en", "status": "APPROVED", "category": "UTILITY",
            "components": [{"type": "HEADER", "format": "TEXT", "text": "Security alert"},
                           {"type": "BODY", "text": "Intrusion at {{1}} on {{2}} at {{3}}. Details: {{4}}"}]}
NAMED = {"name": "smc_visitor", "language": "en_US", "status": "APPROVED", "category": "UTILITY", "parameter_format": "NAMED",
         "components": [{"type": "BODY", "text": "{{visitor}} is here to meet {{host}}"}]}
PENDING = {"name": "smc_daily", "language": "en", "status": "PENDING", "components": [{"type": "BODY", "text": "Summary {{1}}"}]}


class FakeMeta:
    def __init__(self):
        self.number = "91 98765 43210"
        self.sent, self.tokens = [], []
        self.fail_send = []          # MetaErrors raised by the next sends, in order
        self.fail_info = None
        self.templates = [APPROVED, NAMED, PENDING]
        self.phones = [{"id": "222", "display_phone_number": "91 98765 43210", "verified_name": "SMC Ltd", "quality_rating": "GREEN"}]
        self.subscribed = self.registered = None

    def __call__(self, method, path, token, body=None, query=None):
        self.tokens.append(token)
        if path == "oauth/access_token":
            if query["code"] == "expired":
                raise whatsapp.MetaError("This authorization code has expired.", code=100, http_status=400)
            assert query["client_secret"] == "APP-SECRET"
            return {"access_token": "BUSINESS-TOKEN", "token_type": "bearer"}
        if method == "GET" and path == "222":
            if self.fail_info:
                raise whatsapp.MetaError(self.fail_info, code=190, http_status=401)
            return {"display_phone_number": self.number, "verified_name": "SMC Ltd", "quality_rating": "GREEN"}
        if method == "GET" and path == "111":
            return {"name": "SMC Business", "account_review_status": "APPROVED"}
        if path == "111/phone_numbers":
            return {"data": self.phones}
        if path == "111/subscribed_apps":
            self.subscribed = token
            return {"success": True}
        if path == "222/register":
            self.registered = body["pin"]
            return {"success": True}
        if path == "111/message_templates":
            return {"data": self.templates, "paging": {"cursors": {"after": "x"}}}
        if path == "222/messages":
            if self.fail_send:
                raise self.fail_send.pop(0)
            self.sent.append(body)
            return {"messages": [{"id": f"wamid.{len(self.sent)}"}]}
        raise AssertionError((method, path))


def _clear_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def meta(isolated_db, monkeypatch):
    """Manual setup: credentials in the environment."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "ENV-TOKEN")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "222")
    monkeypatch.setenv("WHATSAPP_BUSINESS_ACCOUNT_ID", "111")
    alerts.init_db()
    whatsapp.init_db()
    fake = FakeMeta()
    monkeypatch.setattr(whatsapp.graph, "call", fake)
    return fake


@pytest.fixture
def embedded(isolated_db, monkeypatch):
    """Embedded Signup set up on the server, nothing connected yet."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("META_APP_ID", "1234")
    monkeypatch.setenv("META_APP_SECRET", "APP-SECRET")
    monkeypatch.setenv("META_EMBEDDED_SIGNUP_CONFIG_ID", "5678")
    monkeypatch.setenv("SMC_CREDENTIALS_KEY", Fernet.generate_key().decode())
    alerts.init_db()
    whatsapp.init_db()
    fake = FakeMeta()
    monkeypatch.setattr(whatsapp.graph, "call", fake)
    return fake


def connect(number="9876543210"):
    whatsapp.start_connect("+91", number)
    return whatsapp.verify()


def opted_in(name="Asha", number="9000000001", role="Admin"):
    r = whatsapp.add_recipient(name, "+91", number, role)
    return whatsapp.record_opt_in(r["id"], "written", "admin@smc")


def inbound(phone, text="hi", pnid="222", ts=None):
    return whatsapp.handle_webhook({"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": pnid},
        "messages": [{"from": phone, "type": "text", "text": {"body": text}, "timestamp": str(ts or int(time.time()))}]}}]}]})


# ---- connection --------------------------------------------------------------------------------

def test_without_any_setup_it_is_demo_mode_and_nothing_is_sent(isolated_db, monkeypatch):
    _clear_env(monkeypatch)
    whatsapp.init_db()
    calls = []
    monkeypatch.setattr(whatsapp.graph, "call", lambda *a, **k: calls.append(a))
    c = whatsapp.start_connect("+91", "98765 43210")
    assert c["status"] == "not_configured" and c["demo_mode"] and not c["setup"]["embedded_signup"]
    c = whatsapp.verify()
    assert c["status"] == "not_configured" and c["business_number"] is None
    assert c["state"] == "not_configured" and c["setup"]["manual_missing"] == ["WHATSAPP_ACCESS_TOKEN", "WHATSAPP_PHONE_NUMBER_ID"]
    assert "WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID" in c["last_error"]
    rec = opted_in()
    m = whatsapp.send_test(rec["id"])
    assert m["status"] == "not_sent" and m["error"] == "WhatsApp is not connected"
    assert calls == [] and whatsapp.stats()["sent"] == 0


def test_manual_setup_is_connected_only_when_meta_confirms_the_same_number(meta):
    assert whatsapp.start_connect("+91", "9876543210")["status"] == "pending"
    c = whatsapp.verify()
    assert c["status"] == "connected" and c["mode"] == "manual" and c["verified_name"] == "SMC Ltd"
    assert c["business_account"] == {"name": "SMC Business", "review_status": "APPROVED"}
    meta.number = "91 11111 22222"                                  # Meta's number differs from the one entered
    assert whatsapp.verify()["status"] == "failed"
    meta.number, meta.fail_info = "91 98765 43210", "Invalid OAuth access token"
    c = whatsapp.verify()
    assert c["status"] == "failed" and "Invalid OAuth" in c["last_error"] and c["business_number"] is None
    meta.fail_info = None
    assert whatsapp.verify()["status"] == "connected"
    assert whatsapp.disconnect()["status"] == "disconnected"
    assert set(meta.tokens) == {"ENV-TOKEN"}


def test_the_four_states_come_from_meta_not_from_the_environment(meta, monkeypatch):
    c = whatsapp.connection()
    assert c["state"] == "configured" and c["status"] != "connected"      # credentials exist, not confirmed yet
    assert connect()["state"] == "connected"
    meta.fail_info = "Invalid OAuth access token"
    assert whatsapp.verify()["state"] == "connection_failed"
    meta.fail_info = None
    assert whatsapp.verify()["state"] == "connected"
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "333")                 # credentials changed after the check
    c = whatsapp.connection()
    assert c["state"] == "configured" and c["business_number"] is None and "changed" in c["last_error"]
    monkeypatch.delenv("WHATSAPP_PHONE_NUMBER_ID")
    monkeypatch.delenv("WHATSAPP_ACCESS_TOKEN")
    assert whatsapp.connection()["state"] == "not_configured"


def test_a_token_meta_rejects_while_sending_marks_the_connection_failed(meta):
    connect()
    rec = opted_in()
    inbound(rec["phone"])
    meta.fail_send = [whatsapp.MetaError("Error validating access token: Session has expired", code=190, http_status=401)]
    assert whatsapp.send_test(rec["id"])["status"] == "failed"
    c = whatsapp.connection()
    assert c["state"] == "connection_failed" and "rejected the access token" in c["last_error"]


def test_no_token_or_secret_in_connection_responses(meta, embedded, api, admin_token, monkeypatch):
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "ENV-TOKEN")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "222")
    h = bearer(admin_token)
    bodies = [api.post("/api/connect/verify", headers=h).text, api.get("/api/connect/overview", headers=h).text]
    assert "connected" in bodies[0]
    for b in bodies:
        assert "ENV-TOKEN" not in b and "APP-SECRET" not in b and os.environ["SMC_CREDENTIALS_KEY"] not in b


def test_embedded_signup_stores_the_token_encrypted_and_uses_it(embedded, isolated_db):
    c = whatsapp.connection()
    assert c["status"] == "not_connected" and not c["demo_mode"] and c["setup"]["embedded_signup"]
    c = whatsapp.complete_embedded_signup("CODE", "111", "222", pin="123456")
    assert c["status"] == "connected" and c["mode"] == "embedded_signup" and c["business_number"] == "91 98765 43210"
    assert embedded.subscribed == "BUSINESS-TOKEN" and embedded.registered == "123456"
    assert {t["name"] for t in whatsapp.list_templates()} == {"smc_intrusion_alert", "smc_visitor", "smc_daily"}  # synced
    with sqlite3.connect(isolated_db) as conn:
        dump = "\n".join(conn.iterdump())
    assert "BUSINESS-TOKEN" not in dump                               # never stored in plain text
    rec = opted_in()
    inbound(rec["phone"])
    assert whatsapp.send_test(rec["id"])["status"] == "sent" and embedded.tokens[-1] == "BUSINESS-TOKEN"
    assert whatsapp.verify()["status"] == "connected"                 # re-check with the stored token
    whatsapp.disconnect()
    with sqlite3.connect(isolated_db) as conn:
        assert conn.execute("SELECT token_enc FROM wa_business_accounts").fetchone()[0] is None
    assert whatsapp.verify()["status"] == "not_connected"


def test_embedded_signup_refuses_what_meta_did_not_authorize(embedded, monkeypatch):
    c = whatsapp.complete_embedded_signup("expired", "111", "222")
    assert c["status"] == "failed" and "30 seconds" in c["last_error"]
    c = whatsapp.complete_embedded_signup("CODE", "111", "999")       # a number from another account
    assert c["status"] == "failed" and "not in the WhatsApp Business Account" in c["last_error"]
    with pytest.raises(ValueError, match="6 digits"):
        whatsapp.complete_embedded_signup("CODE", "111", "222", pin="12")
    monkeypatch.delenv("SMC_CREDENTIALS_KEY")
    with pytest.raises(whatsapp.NotConfigured, match="SMC_CREDENTIALS_KEY"):
        whatsapp.complete_embedded_signup("CODE", "111", "222")


def test_a_changed_encryption_key_is_reported_not_guessed(embedded, monkeypatch):
    whatsapp.complete_embedded_signup("CODE", "111", "222")
    monkeypatch.setenv("SMC_CREDENTIALS_KEY", Fernet.generate_key().decode())
    c = whatsapp.verify()
    assert c["status"] == "failed" and "SMC_CREDENTIALS_KEY" in c["last_error"]
    with pytest.raises(credential_store.Undecryptable):
        credential_store.decrypt(Fernet(Fernet.generate_key()).encrypt(b"x").decode())


# ---- recipients, opt-in, rules ------------------------------------------------------------------

def test_phone_and_recipient_validation(meta):
    with pytest.raises(ValueError):
        whatsapp.normalize_phone("", "9876543210")
    with pytest.raises(ValueError):
        whatsapp.normalize_phone("+91", "123")
    assert whatsapp.normalize_phone("+91", "098765 43210") == "919876543210"
    r = whatsapp.add_recipient("Ravi", "+91", "9000000002", "Security Manager")
    assert r["opt_in"]["status"] == "pending"
    with pytest.raises(ValueError, match="already"):
        whatsapp.add_recipient("Ravi again", "91", "9000000002", "Admin")
    with pytest.raises(ValueError, match="Role"):
        whatsapp.add_recipient("X", "+91", "9000000003", "Owner")
    with pytest.raises(ValueError, match="consent"):
        whatsapp.record_opt_in(r["id"], "whatsapp_reply", "admin")     # only Meta's webhook records a reply


def test_only_opted_in_recipients_get_messages(meta):
    connect()
    r = whatsapp.add_recipient("Asha", "+91", "9000000001", "Admin")
    m = whatsapp.send_test(r["id"])
    assert m["status"] == "not_sent" and "not opted in" in m["error"] and meta.sent == []
    whatsapp.record_opt_in(r["id"], "written", "admin@smc")
    inbound(r["phone"])
    assert whatsapp.send_test(r["id"])["status"] == "sent"
    whatsapp.record_opt_out(r["id"], "admin@smc")
    m = whatsapp.send_test(r["id"])
    assert m["status"] == "not_sent" and "opted out" in m["error"] and len(meta.sent) == 1


def test_rules_texts_and_validation(meta):
    r = whatsapp.get_rule("intrusion")
    assert not r["enabled"] and r["priority"] == "High" and "{camera}" in r["template"] and "CAM2" in r["preview"]
    with pytest.raises(ValueError, match="recipient"):
        whatsapp.update_rule("intrusion", enabled=True)
    rec = opted_in()
    r = whatsapp.update_rule("intrusion", enabled=True, recipient_ids=[rec["id"]], template="Alert at {camera} ({unknown})")
    assert r["enabled"] and r["customized"] and r["preview"] == "Alert at CAM2 ({unknown})"
    assert whatsapp.update_rule("intrusion", reset_template=True)["customized"] is False
    with pytest.raises(ValueError, match="22:00"):
        whatsapp.update_rule("intrusion", preferences={"quiet_from": "25:00", "quiet_to": "06:00"})
    with pytest.raises(ValueError, match="both"):
        whatsapp.update_rule("intrusion", preferences={"quiet_from": "22:00", "quiet_to": ""})


# ---- what Meta accepts: templates and the 24-hour window ----------------------------------------

def test_without_a_template_or_open_window_nothing_is_sent_and_meta_is_not_called(meta):
    connect()
    rec = opted_in()
    m = whatsapp.send_test(rec["id"], "intrusion")
    assert m["status"] == "not_sent" and "24 hours" in m["error"] and "template" in m["error"] and meta.sent == []
    assert inbound(rec["phone"])["messages"] == 1                       # they messaged us: free text is allowed for 24 h
    assert whatsapp.list_recipients()[0]["window_open"]
    m = whatsapp.send_test(rec["id"], "intrusion")
    assert m["status"] == "sent" and m["message_type"] == "text" and meta.sent[0]["type"] == "text"
    last = whatsapp.list_recipients()[0]["last_inbound_at"]
    inbound(rec["phone"], ts=int(time.time()) - 25 * 3600)              # a late-arriving older message doesn't move it back
    assert whatsapp.list_recipients()[0]["last_inbound_at"] == last


def test_rules_send_through_an_approved_template(meta):
    connect()
    rec = opted_in()
    whatsapp.sync_templates()
    with pytest.raises(ValueError, match="approved"):
        whatsapp.update_rule("daily_summary", meta_template={"name": "smc_daily", "language": "en", "params": ["summary"]})
    with pytest.raises(ValueError, match="4 variable"):
        whatsapp.update_rule("intrusion", meta_template={"name": "smc_intrusion_alert", "language": "en", "params": ["zone"]})
    r = whatsapp.update_rule("intrusion", enabled=True, recipient_ids=[rec["id"]], preferences={"cooldown_minutes": 0},
                             meta_template={"name": "smc_intrusion_alert", "language": "en", "params": ["zone", "camera", "time", "message"]})
    assert r["meta_template"]["usable"] and "Reception Entry Gate on CAM2" in r["meta_template"]["preview"]
    (m,) = whatsapp.notify("intrusion", {"zone": "Main Door", "camera": "CAM1", "time": "10:00", "message": "1 person\ninside   Main Door"},
                           source_ref="alert:1")
    assert m["status"] == "queued" and m["message_type"] == "template"
    assert whatsapp.process_queue() == 1
    t = meta.sent[0]["template"]
    assert meta.sent[0]["type"] == "template" and t["name"] == "smc_intrusion_alert" and t["language"] == {"code": "en"}
    assert [p["text"] for p in t["components"][0]["parameters"]] == ["Main Door", "CAM1", "10:00", "1 person · inside   Main Door"]
    assert whatsapp.history()[0]["body"] == "Intrusion at Main Door on CAM1 at 10:00. Details: 1 person · inside   Main Door"
    whatsapp.update_rule("visitor", enabled=True, recipient_ids=[rec["id"]],
                         meta_template={"name": "smc_visitor", "language": "en_US", "params": ["visitor", "host"]})
    whatsapp.notify("visitor", {"visitor": "Rahul", "host": "Ravi"}, source_ref="visit:1")
    whatsapp.process_queue()
    assert meta.sent[1]["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "Rahul", "parameter_name": "visitor"}, {"type": "text", "text": "Ravi", "parameter_name": "host"}]
    # Meta withdraws the template: the rule stops using it.
    whatsapp.handle_webhook({"entry": [{"id": "111", "changes": [{"field": "message_template_status_update", "value": {
        "event": "PAUSED", "message_template_name": "smc_intrusion_alert", "message_template_language": "en"}}]}]})
    assert not whatsapp.get_rule("intrusion")["meta_template"]["usable"]


# ---- queue, retries, duplicates ---------------------------------------------------------------

def _rule_ready(meta, key="loitering"):
    connect()
    rec = opted_in()
    inbound(rec["phone"])
    whatsapp.update_rule(key, enabled=True, recipient_ids=[rec["id"]], priority="High", preferences={"cooldown_minutes": 0})
    return rec


def test_temporary_meta_errors_are_retried_with_backoff_permanent_ones_are_not(meta):
    _rule_ready(meta)
    meta.fail_send = [whatsapp.MetaError("Rate limit hit", code=130429, http_status=400, transient=True),
                      whatsapp.MetaError("Could not reach Meta: timed out", transient=True)]
    t0 = time.time()
    whatsapp.notify("loitering", {"zone": "Lobby"}, source_ref="alert:7", now=t0)
    whatsapp.process_queue(now=t0)
    (m,) = whatsapp.history()
    assert m["status"] == "retrying" and m["attempts"] == 1 and m["next_attempt_at"] == pytest.approx(t0 + 30)
    assert whatsapp.process_queue(now=t0 + 10) == 0                      # not due yet
    whatsapp.process_queue(now=t0 + 31)
    assert whatsapp.history()[0]["status"] == "retrying" and whatsapp.history()[0]["attempts"] == 2
    whatsapp.process_queue(now=t0 + 200)
    m = whatsapp.history()[0]
    assert m["status"] == "sent" and m["attempts"] == 3 and m["has_meta_id"]
    meta.fail_send = [whatsapp.MetaError("(#131026) Message undeliverable", code=131026, http_status=400)]
    whatsapp.notify("loitering", {"zone": "Lobby"}, source_ref="alert:8", now=t0)
    whatsapp.process_queue(now=t0 + 300)
    m = whatsapp.history()[0]
    assert m["status"] == "failed" and m["attempts"] == 1 and "undeliverable" in m["error"]


def test_retries_stop_after_the_last_attempt(meta):
    _rule_ready(meta)
    meta.fail_send = [whatsapp.MetaError("Service unavailable", http_status=503, transient=True)] * whatsapp.MAX_ATTEMPTS
    t = time.time()
    whatsapp.notify("loitering", {"zone": "Lobby"}, source_ref="alert:9", now=t)
    for _ in range(whatsapp.MAX_ATTEMPTS + 2):
        t += 4000
        whatsapp.process_queue(now=t)
    m = whatsapp.history()[0]
    assert m["status"] == "failed" and m["attempts"] == whatsapp.MAX_ATTEMPTS and meta.sent == []


def test_the_same_event_is_never_sent_twice(meta):
    _rule_ready(meta)
    assert len(whatsapp.notify("loitering", {"zone": "Lobby"}, source_ref="alert:5")) == 1
    assert whatsapp.notify("loitering", {"zone": "Lobby"}, source_ref="alert:5") == []    # duplicate
    whatsapp.process_queue()
    whatsapp.process_queue()
    assert len(meta.sent) == 1


def test_quiet_hours_cooldown_and_priority(meta):
    _rule_ready(meta)
    whatsapp.update_rule("loitering", priority="Normal", preferences={"quiet_from": "00:00", "quiet_to": "23:59", "cooldown_minutes": 5})
    assert whatsapp.notify("loitering", {"zone": "Waiting"}) == []          # quiet hours hold a Normal rule
    whatsapp.update_rule("loitering", priority="High")
    assert len(whatsapp.notify("loitering", {"zone": "Waiting"})) == 1      # High always goes
    assert whatsapp.notify("loitering", {"zone": "Waiting"}) == []          # cooldown
    whatsapp.update_rule("loitering", enabled=False)
    assert whatsapp.notify("loitering", {"zone": "Waiting"}, now=time.time() + 3600) == []


def test_dispatcher_queues_new_alerts_once_and_skips_the_backlog(meta):
    rec = _rule_ready(meta, "intrusion")
    cam = camera_db.add_camera("CAM2", "HQ")
    alerts.raise_alert("Intrusion Detected", "High", cam, "old one", now=time.time() - 60)
    d = whatsapp.Dispatcher()
    d._prime()                                                               # start from now: the old alert is not sent
    alerts.raise_alert("Intrusion Detected", "High", cam, "1 person inside Main Door", zone_name="Main Door")
    alerts.raise_alert("Camera Offline", "Critical", cam, "not a rule")
    d.run_once()
    assert meta.sent == []                                                   # queued, the sender sends it
    whatsapp.process_queue()
    d.run_once()
    whatsapp.process_queue()
    assert len(meta.sent) == 1 and meta.sent[0]["to"] == rec["phone"]
    assert "Main Door" in meta.sent[0]["text"]["body"] and "CAM2" in meta.sent[0]["text"]["body"]


# ---- webhook --------------------------------------------------------------------------------------

def test_webhook_needs_the_signature_and_updates_statuses_and_opt_outs(api, meta, monkeypatch):
    rec = _rule_ready(meta, "visitor")
    whatsapp.notify("visitor", {"visitor": "Rahul"}, source_ref="visit:1")
    whatsapp.process_queue()
    payload = json.dumps({"entry": [{"changes": [{"field": "messages", "value": {
        "metadata": {"phone_number_id": "222"},
        "statuses": [{"id": "wamid.1", "status": "delivered", "timestamp": "1"}],
        "messages": [{"from": rec["phone"], "type": "text", "text": {"body": "stop"}, "timestamp": "2"}]}}]}]}).encode()
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "shh")
    assert api.post("/api/connect/webhook", content=payload, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 401
    assert api.post("/api/connect/webhook", content=payload).status_code == 401
    sig = "sha256=" + hmac.new(b"shh", payload, hashlib.sha256).hexdigest()
    r = api.post("/api/connect/webhook", content=payload, headers={"x-hub-signature-256": sig}).json()
    assert r["statuses"] == 1 and r["messages"] == 1
    s = whatsapp.stats()
    assert (s["sent"], s["delivered"]) == (1, 1) and s["last_success"]["recipient_name"] == "Asha"
    assert whatsapp.get_recipient(rec["id"])["opt_in"]["status"] == "opted_out"           # STOP
    inbound(rec["phone"], "START")
    assert whatsapp.get_recipient(rec["id"])["opt_in"]["method"] == "whatsapp_reply"
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "vt")
    ok = api.get("/api/connect/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert api.get("/api/connect/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"}).status_code == 403


# ---- organizations ------------------------------------------------------------------------------

def test_organizations_never_see_or_change_each_others_data(meta, monkeypatch):
    rec = _rule_ready(meta, "visitor")
    whatsapp.notify("visitor", {"visitor": "Rahul"}, source_ref="visit:1")
    whatsapp.process_queue()
    monkeypatch.setenv("SMC_ORG_ID", "acme")
    whatsapp.init_db()
    assert whatsapp.list_recipients() == [] and whatsapp.history() == [] and whatsapp.stats()["sent"] == 0
    assert whatsapp.connection()["status"] == "not_connected" and not whatsapp.get_rule("visitor")["enabled"]
    assert whatsapp.get_recipient(rec["id"]) is None
    assert whatsapp.update_recipient(rec["id"], name="Hijacked") is None
    whatsapp.delete_recipient(rec["id"])
    with pytest.raises(ValueError, match="Unknown recipient"):
        whatsapp.update_rule("visitor", recipient_ids=[rec["id"]])
    with pytest.raises(ValueError, match="Unknown recipient"):
        whatsapp.send_test(rec["id"])
    # acme can't take the default organization's number, and webhooks for it stay with its owner
    assert whatsapp.verify()["status"] == "failed"
    other = whatsapp.add_recipient("Asha (acme)", "+91", "9000000001", "Admin")      # same phone, other organization: allowed
    whatsapp.handle_webhook({"entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "222"},
                                                               "messages": [{"from": "919000000001", "text": {"body": "STOP"}}]}}]}]})
    assert whatsapp.get_recipient(other["id"])["opt_in"]["status"] == "pending"
    monkeypatch.setenv("SMC_ORG_ID", "default")
    assert whatsapp.get_recipient(rec["id"])["name"] == "Asha" and whatsapp.get_recipient(rec["id"])["opt_in"]["status"] == "opted_out"
    assert len(whatsapp.history()) == 1


def test_first_version_tables_are_upgraded_without_losing_rows(isolated_db, monkeypatch):
    _clear_env(monkeypatch)
    with sqlite3.connect(isolated_db) as conn:
        conn.executescript("""
            CREATE TABLE wa_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE wa_recipients (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
            CREATE TABLE wa_rules (event_key TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, recipient_ids TEXT NOT NULL DEFAULT '[]',
                priority TEXT NOT NULL, preferences TEXT NOT NULL DEFAULT '{}', template TEXT, updated_at REAL);
            CREATE TABLE wa_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, event_key TEXT NOT NULL,
                recipient_id INTEGER, recipient_name TEXT, phone TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL, wamid TEXT,
                error TEXT, is_test INTEGER NOT NULL DEFAULT 0, source_ref TEXT, status_at REAL);
            INSERT INTO wa_recipients VALUES (7, 'Neha', '919000000009', 'HR Manager', 1, 1);
            INSERT INTO wa_rules VALUES ('visitor', 1, '[7]', 'High', '{"cooldown_minutes": 2}', 'Hi {visitor}', 1);
            INSERT INTO wa_messages (ts, event_key, recipient_id, recipient_name, phone, body, status) VALUES (1, 'visitor', 7, 'Neha', '919000000009', 'x', 'sent');
            INSERT INTO wa_settings VALUES ('connection', '{"status": "failed", "entered_number": "919876543210", "last_error": "bad token"}');
        """)
    whatsapp.init_db()
    whatsapp.init_db()                                                       # twice is harmless
    (r,) = whatsapp.list_recipients()
    assert r["id"] == 7 and r["name"] == "Neha" and r["opt_in"]["status"] == "pending"
    v = whatsapp.get_rule("visitor")
    assert v["enabled"] and v["recipient_ids"] == [7] and v["priority"] == "High" and v["template"] == "Hi {visitor}"
    assert len(whatsapp.history()) == 1 and whatsapp.get_rule("intrusion") is not None
    c = whatsapp.connection()
    assert c["entered_number"] == "919876543210" and c["last_error"] == "bad token"


# ---- API ------------------------------------------------------------------------------------------

def test_api_needs_admin_and_never_exposes_credentials(api, admin_token, embedded):
    assert api.get("/api/connect/overview").status_code == 401
    assert api.post("/api/connect/embedded-signup", json={"code": "c", "waba_id": "1", "phone_number_id": "2"}).status_code == 401
    h = bearer(admin_token)
    ov = api.get("/api/connect/overview", headers=h).json()
    assert ov["embedded_signup"] == {"available": True, "missing": [], "app_id": "1234", "config_id": "5678", "graph_version": whatsapp.api_version()}
    r = api.post("/api/connect/embedded-signup", json={"code": "CODE", "waba_id": "111", "phone_number_id": "222'; --"}, headers=h)
    assert r.status_code == 422                                              # Meta ids are numeric
    r = api.post("/api/connect/embedded-signup", json={"code": "CODE", "waba_id": "111", "phone_number_id": "222"}, headers=h)
    assert r.json()["status"] == "connected"
    text = json.dumps([api.get(p, headers=h).json() for p in ("/api/connect/overview", "/api/connect/rules", "/api/connect/templates",
                                                               "/api/connect/recipients", "/api/connect/history")])
    assert "BUSINESS-TOKEN" not in text and "APP-SECRET" not in text
    rid = api.post("/api/connect/recipients", json={"name": "Asha", "country_code": "+91", "number": "9000000001", "role": "Reception"},
                   headers=h).json()["id"]
    assert api.post("/api/connect/test", json={"country_code": "+91", "number": "9111111111"}, headers=h).status_code == 422
    assert api.post("/api/connect/test", json={"recipient_id": rid}, headers=h).json()["status"] == "not_sent"
    assert api.post(f"/api/connect/recipients/{rid}/opt-in", json={"method": "written"}, headers=h).status_code == 422
    assert api.post(f"/api/connect/recipients/{rid}/opt-in", json={"method": "written", "confirmed": True},
                    headers=h).json()["opt_in"]["status"] == "opted_in"
    assert api.post("/api/connect/templates/sync", headers=h).status_code == 200
    r = api.patch("/api/connect/rules/daily_summary", json={"meta_template": {"name": "smc_daily", "language": "en", "params": ["summary"]}}, headers=h)
    assert r.status_code == 422 and "approved" in r.json()["detail"]
    assert api.patch("/api/connect/rules/nope", json={"enabled": True}, headers=h).status_code == 404
    assert api.get("/api/connect/history?status=nope", headers=h).status_code == 422
