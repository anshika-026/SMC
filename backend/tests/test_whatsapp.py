"""SMC Connect (whatsapp.py, whatsapp_routes.py) with a fake Meta Cloud API."""

import hashlib
import hmac
import json
import time

import pytest

from app import alerts, camera_db, whatsapp
from tests.conftest import bearer


class FakeMeta:
    def __init__(self, number="91 98765 43210"):
        self.number = number
        self.sent = []
        self.fail_send = None
        self.fail_info = None

    def __call__(self, method, path, body=None):
        if path.startswith("PNID?"):
            if self.fail_info:
                raise whatsapp.MetaError(self.fail_info)
            return {"display_phone_number": self.number, "verified_name": "SMC Ltd", "quality_rating": "GREEN"}
        if path.startswith("WABA?"):
            return {"name": "SMC Business", "account_review_status": "APPROVED"}
        if path == "PNID/messages":
            if self.fail_send:
                raise whatsapp.MetaError(self.fail_send)
            self.sent.append(body)
            return {"messages": [{"id": f"wamid.{len(self.sent)}"}]}
        raise AssertionError(path)


@pytest.fixture
def wa(isolated_db, monkeypatch):
    alerts.init_db()
    whatsapp.init_db()
    c = whatsapp.MetaCloudClient()
    c.token, c.phone_number_id, c.waba_id = "TOKEN", "PNID", "WABA"
    fake = FakeMeta()
    monkeypatch.setattr(c, "_call", fake)
    monkeypatch.setattr(whatsapp, "client", c)
    return fake


@pytest.fixture
def unconfigured(isolated_db, monkeypatch):
    whatsapp.init_db()
    monkeypatch.setattr(whatsapp, "client", whatsapp.MetaCloudClient.__new__(whatsapp.MetaCloudClient))
    whatsapp.client.token = whatsapp.client.phone_number_id = whatsapp.client.waba_id = ""
    whatsapp.client.version = "v21.0"


def connect(number="9876543210"):
    whatsapp.start_connect("+91", number)
    return whatsapp.verify()


def test_without_credentials_it_is_demo_mode_and_never_connected(unconfigured):
    c = whatsapp.start_connect("+91", "98765 43210")
    assert c["status"] == "not_configured" and c["demo_mode"]
    c = whatsapp.verify()
    assert c["status"] == "not_configured" and c["business_number"] is None and "Demo mode" in c["last_error"]
    rec = whatsapp.add_recipient("Asha", "+91", "9000000001", "Admin")
    m = whatsapp.send_test(rec["id"])
    assert m["status"] == "not_sent" and m["error"] == "WhatsApp is not connected"
    assert whatsapp.stats()["sent"] == 0


def test_connected_only_when_meta_confirms_the_same_number(wa):
    assert whatsapp.start_connect("+91", "9876543210")["status"] == "pending"
    c = whatsapp.verify()
    assert c["status"] == "connected" and c["verified_name"] == "SMC Ltd" and c["business_account"]["review_status"] == "APPROVED"
    wa.number = "91 11111 22222"                                   # Meta's number differs from the one entered
    assert whatsapp.verify()["status"] == "failed"
    wa.number, wa.fail_info = "91 98765 43210", "Invalid OAuth access token"
    c = whatsapp.verify()
    assert c["status"] == "failed" and "Invalid OAuth" in c["last_error"] and c["business_number"] is None
    wa.fail_info = None
    assert whatsapp.verify()["status"] == "connected"
    assert whatsapp.disconnect()["status"] == "disconnected"


def test_phone_and_recipient_validation(wa):
    with pytest.raises(ValueError):
        whatsapp.normalize_phone("", "9876543210")
    with pytest.raises(ValueError):
        whatsapp.normalize_phone("+91", "123")
    assert whatsapp.normalize_phone("+91", "098765 43210") == "919876543210"
    whatsapp.add_recipient("Ravi", "+91", "9000000002", "Security Manager")
    with pytest.raises(ValueError, match="already"):
        whatsapp.add_recipient("Ravi again", "91", "9000000002", "Admin")
    with pytest.raises(ValueError, match="Role"):
        whatsapp.add_recipient("X", "+91", "9000000003", "Owner")


def test_rules_templates_and_validation(wa):
    r = whatsapp.get_rule("intrusion")
    assert not r["enabled"] and r["priority"] == "High" and "{camera}" in r["template"] and "CAM2" in r["preview"]
    with pytest.raises(ValueError, match="recipient"):
        whatsapp.update_rule("intrusion", enabled=True)            # nobody to send to
    rec = whatsapp.add_recipient("Asha", "+91", "9000000001", "Admin")
    r = whatsapp.update_rule("intrusion", enabled=True, recipient_ids=[rec["id"]], template="Alert at {camera} ({unknown})")
    assert r["enabled"] and r["customized"] and r["preview"] == "Alert at CAM2 ({unknown})"
    assert whatsapp.update_rule("intrusion", reset_template=True)["customized"] is False
    with pytest.raises(ValueError, match="22:00"):
        whatsapp.update_rule("intrusion", preferences={"quiet_from": "25:00", "quiet_to": "06:00"})
    with pytest.raises(ValueError, match="both"):
        whatsapp.update_rule("intrusion", preferences={"quiet_from": "22:00", "quiet_to": ""})
    assert whatsapp.get_rule("daily_summary")["preferences"]["send_at"] == "20:00"


def test_real_outcome_of_each_send(wa):
    connect()
    rec = whatsapp.add_recipient("Asha", "+91", "9000000001", "Admin")
    m = whatsapp.send_test(rec["id"])
    assert m["status"] == "sent" and m["has_meta_id"] and wa.sent[0]["to"] == "919000000001"
    wa.fail_send = "Recipient phone number not in allowed list"
    m = whatsapp.send_test(rec["id"])
    assert m["status"] == "failed" and "allowed list" in m["error"]
    s = whatsapp.stats()
    assert (s["sent"], s["failed"]) == (0, 0)                     # tests are not counted in the statistics
    assert [h["status"] for h in whatsapp.history()] == ["failed", "sent"]


def test_quiet_hours_cooldown_and_priority(wa):
    connect()
    rec = whatsapp.add_recipient("Asha", "+91", "9000000001", "Admin")
    whatsapp.update_rule("loitering", enabled=True, recipient_ids=[rec["id"]], priority="Normal",
                         preferences={"quiet_from": "00:00", "quiet_to": "23:59", "cooldown_minutes": 5})
    assert whatsapp.notify("loitering", {"zone": "Waiting"}) == []          # quiet hours hold a Normal rule
    whatsapp.update_rule("loitering", priority="High")
    assert len(whatsapp.notify("loitering", {"zone": "Waiting"})) == 1      # High always goes
    assert whatsapp.notify("loitering", {"zone": "Waiting"}) == []          # cooldown
    whatsapp.update_rule("loitering", enabled=False)
    assert whatsapp.notify("loitering", {"zone": "Waiting"}, now=time.time() + 3600) == []


def test_dispatcher_sends_new_alerts_once_and_skips_the_backlog(wa, monkeypatch):
    connect()
    cam = camera_db.add_camera("CAM2", "HQ")
    rec = whatsapp.add_recipient("Asha", "+91", "9000000001", "Security Manager")
    whatsapp.update_rule("intrusion", enabled=True, recipient_ids=[rec["id"]], preferences={"cooldown_minutes": 0})
    old = alerts.raise_alert("Intrusion Detected", "High", cam, "old one", now=time.time() - 60)
    d = whatsapp.Dispatcher()
    d._prime()                                                               # start from now: the old alert is not sent
    alerts.raise_alert("Intrusion Detected", "High", cam, "1 person inside Main Door", zone_name="Main Door")
    alerts.raise_alert("Camera Offline", "Critical", cam, "not a rule")
    d.run_once()
    d.run_once()                                                             # nothing twice
    assert len(wa.sent) == 1 and "Main Door" in wa.sent[0]["text"]["body"] and "CAM2" in wa.sent[0]["text"]["body"]
    assert old and whatsapp.history()[0]["event_key"] == "intrusion"


def test_webhook_updates_delivery_status_only_when_signed(api, wa, monkeypatch):
    connect()
    rec = whatsapp.add_recipient("Asha", "+91", "9000000001", "Admin")
    whatsapp.update_rule("visitor", enabled=True, recipient_ids=[rec["id"]])
    (m,) = whatsapp.notify("visitor", {"visitor": "Rahul"})
    payload = json.dumps({"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.1", "status": "delivered", "timestamp": "1"}]}}]}]}).encode()
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "shh")
    assert api.post("/api/connect/webhook", content=payload, headers={"x-hub-signature-256": "sha256=bad"}).status_code == 401
    sig = "sha256=" + hmac.new(b"shh", payload, hashlib.sha256).hexdigest()
    assert api.post("/api/connect/webhook", content=payload, headers={"x-hub-signature-256": sig}).json() == {"updated": 1}
    s = whatsapp.stats()
    assert (s["sent"], s["delivered"]) == (1, 1) and s["last_success"]["recipient_name"] == "Asha"
    monkeypatch.setenv("WHATSAPP_WEBHOOK_VERIFY_TOKEN", "vt")
    ok = api.get("/api/connect/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.text == "42"
    assert api.get("/api/connect/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "no", "hub.challenge": "42"}).status_code == 403


def test_api_needs_admin_and_never_exposes_credentials(api, admin_token, wa):
    assert api.get("/api/connect/overview").status_code == 401
    h = bearer(admin_token)
    api.post("/api/connect/connect", json={"country_code": "+91", "number": "9876543210"}, headers=h)
    ov = api.post("/api/connect/verify", headers=h).json()
    assert ov["status"] == "connected"
    text = json.dumps(api.get("/api/connect/overview", headers=h).json())
    assert "TOKEN" not in text and "PNID" not in text and "WABA" not in text
    r = api.post("/api/connect/recipients", json={"name": "Asha", "country_code": "+91", "number": "9000000001", "role": "Reception"}, headers=h)
    assert r.status_code == 200 and r.json()["phone_masked"].startswith("+91")
    t = api.post("/api/connect/test", json={"recipient_id": r.json()["id"]}, headers=h).json()
    assert t["status"] == "sent"
    assert len(api.get("/api/connect/history?q=asha", headers=h).json()["messages"]) == 1
    assert api.patch("/api/connect/rules/nope", json={"enabled": True}, headers=h).status_code == 404
    assert api.post("/api/connect/preview", json={"key": "visitor", "template": "Hi {visitor}"}, headers=h).json()["preview"] == "Hi Rahul Mehta"
