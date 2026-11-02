"""Visitor records (visitor_registry.py): profiles vs visits, unique IDs,
returning-visitor matching, entry/exit/meeting times with corrections,
waiting time and duration, operator-confirmed camera detections, consented
snapshots (best clear face only), retention and erasure, KPIs, analytics and
exports - plus the HTTP API."""

import datetime
import io
import time
import zipfile

import cv2
import numpy as np
import pytest

from app import camera_db, employee_directory, visitor_registry as reg, visitors
from app.reid import reid_db
from tests.conftest import bearer

ADMIN = {"principal_type": "admin", "email": "reception@example.com"}


@pytest.fixture
def vr(isolated_db, tmp_path, monkeypatch):
    for mod in (reg, visitors):
        monkeypatch.setattr(mod, "DB_PATH", isolated_db)
    monkeypatch.setattr(reg, "MEDIA_DIR", tmp_path / "visitor_records")
    monkeypatch.setattr(employee_directory, "get_employee", {
        "E1": {"name": "Asha", "company": "Sales"},
        "E2": {"name": "Bala", "company": "Accounts"},
    }.get)
    visitors.init_db()
    reid_db.init_db()
    reg.init_db()
    return isolated_db


def register(name="Ravi Kumar", now=None, **kw):
    data = {"name": name, "purpose": "Meeting", "host_employee_id": "E1", **kw}
    return reg.register_visit(data, ADMIN, now=now)


def at(day, hh, mm=0):
    return datetime.datetime.strptime(f"{day} {hh:02d}:{mm:02d}", "%Y-%m-%d %H:%M").timestamp()


# ---------------------------------------------------------------- registration

def test_registration_creates_profile_and_visit_with_unique_ids(vr):
    v = register(company="Acme", mobile="+91 98765 43210", email="Ravi@Acme.com")
    assert v["visitor_code"] == f"VIS-{v['visitor_id']:06d}"
    assert v["visit_code"].startswith("VST-") and v["visit_code"].endswith(f"{v['id']:05d}")
    assert v["status"] == "pending" and v["host_name"] == "Asha" and v["host_department"] == "Sales"
    assert v["first_visit"] is True and v["snapshot_consent"] is False and v["snapshot_status"] == "declined"
    profile = reg.get_profile(v["visitor_id"])
    assert profile["email"] == "ravi@acme.com" and profile["total_visits"] == 1

    v2 = register("Meena", mobile="9000000001")
    assert v2["visitor_code"] != v["visitor_code"] and v2["visit_code"] != v["visit_code"]


@pytest.mark.parametrize("bad, msg", [
    ({"purpose": ""}, "Purpose"),
    ({"host_employee_id": "NOPE"}, "employee list"),
    ({"host_employee_id": None}, "Person to meet"),
    ({"name": " "}, "name is required"),
    ({"mobile": "abc"}, "Mobile"),
    ({"email": "x@"}, "E-mail"),
    ({"visit_date": "30-10-2026"}, "date"),
])
def test_registration_validates_required_fields(vr, bad, msg):
    with pytest.raises(reg.RegistryError, match=msg):
        register(**bad)


def test_returning_visitor_is_matched_and_linked_not_duplicated(vr):
    first = register(mobile="98765 43210", company="Acme")
    with pytest.raises(reg.DuplicateVisitor) as e:
        register("R. Kumar", mobile="+91-9876543210")       # same mobile, different spelling
    assert [m["id"] for m in e.value.matches] == [first["visitor_id"]]
    assert e.value.matches[0]["strength"] == "strong"

    again = register(visitor_id=first["visitor_id"], purpose="Delivery", email="ravi@acme.com")
    assert again["visitor_id"] == first["visitor_id"] and again["first_visit"] is False and again["prior_visits"] == 1
    p = reg.get_profile(first["visitor_id"])
    assert p["total_visits"] == 2 and p["email"] == "ravi@acme.com"   # new detail filled in
    assert len(reg.search_visitors()) == 1

    # A namesake the operator says is a different person.
    other = register("Ravi Kumar", create_new=True, company="Other Co")
    assert other["visitor_id"] != first["visitor_id"]


def test_similar_name_is_offered_as_a_match(vr):
    register("Priya Sharma", company="Acme")
    assert reg.find_matches("Priya Sharmaa", company="Acme")[0]["strength"] == "name"
    assert reg.find_matches("Someone Else") == []


def test_profile_edit_does_not_rewrite_past_visits(vr):
    v = register(company="Acme")
    reg.update_visitor(v["visitor_id"], {"name": "Ravi K", "company": "NewCo"}, ADMIN)
    old = reg.get_visit(v["id"])
    assert old["visitor_name"] == "Ravi Kumar" and old["visitor_company"] == "Acme"
    assert reg.get_profile(v["visitor_id"])["name"] == "Ravi K"


def test_records_persist_across_connections(vr):
    v = register()
    reg.record_time(v["id"], "entry", principal=ADMIN)
    reg.init_db()   # a restart: tables already there, nothing lost
    assert reg.get_visit(v["id"])["status"] == "in"


# ---------------------------------------------------------------- times

def test_waiting_time_and_duration(vr):
    now = time.time()
    v = register(now=now - 3600)
    reg.record_time(v["id"], "entry", now - 3000, ADMIN, now=now)
    v = reg.get_visit(v["id"], now=now)
    assert v["status"] == "in" and v["elapsed_seconds"] == pytest.approx(3000)
    assert v["waiting_seconds"] is None and v["duration_seconds"] is None   # not invented
    reg.record_time(v["id"], "host_notified", now - 2900, ADMIN, now=now)
    reg.record_time(v["id"], "meeting_start", now - 2400, ADMIN, now=now)
    v = reg.record_time(v["id"], "exit", now - 600, ADMIN, now=now)
    assert v["status"] == "out"
    assert v["waiting_seconds"] == pytest.approx(600)          # meeting start - ENTRY, not registration
    assert v["duration_seconds"] == pytest.approx(2400)
    assert v["elapsed_seconds"] is None


def test_time_order_is_enforced(vr):
    now = time.time()
    v = register(now=now - 3600)
    with pytest.raises(reg.RegistryError, match="entry before the exit"):
        reg.record_time(v["id"], "exit", now, ADMIN, now=now)
    with pytest.raises(reg.RegistryError, match="entry before the meeting"):
        reg.record_time(v["id"], "meeting_start", now, ADMIN, now=now)
    reg.record_time(v["id"], "entry", now - 100, ADMIN, now=now)
    with pytest.raises(reg.RegistryError, match="before the visitor arrived"):
        reg.record_time(v["id"], "meeting_start", now - 200, ADMIN, now=now)
    with pytest.raises(reg.RegistryError, match="before the entry"):
        reg.record_time(v["id"], "exit", now - 200, ADMIN, now=now)
    with pytest.raises(reg.RegistryError, match="future"):
        reg.record_time(v["id"], "exit", now + 3600, ADMIN, now=now)
    with pytest.raises(reg.RegistryError, match="already recorded"):
        reg.record_time(v["id"], "entry", now - 50, ADMIN, now=now)


def test_manual_correction_voids_the_old_event_and_needs_a_reason(vr):
    now = time.time()
    v = register(now=now - 7200)
    reg.record_time(v["id"], "entry", now - 3600, ADMIN, now=now)
    with pytest.raises(reg.RegistryError, match="reason"):
        reg.correct_time(v["id"], "entry", now - 4000, "", ADMIN, now=now)
    reg.correct_time(v["id"], "entry", now - 4000, "visitor came in before check-in", ADMIN, now=now)
    # A missed exit, added afterwards.
    v = reg.correct_time(v["id"], "exit", now - 1000, "camera missed the exit", ADMIN, now=now)
    assert v["status"] == "out" and v["duration_seconds"] == pytest.approx(3000)
    assert v["entry_source"] == "correction"
    events = reg.get_visit(v["id"], with_detail=True)["events"]
    entries = [e for e in events if e["kind"] == "entry"]
    assert [e["voided"] for e in entries] == [1, 0] and entries[1]["note"] == "visitor came in before check-in"
    # Clearing a wrongly recorded exit puts them back inside.
    v = reg.correct_time(v["id"], "exit", None, "wrong person", ADMIN, now=now)
    assert v["status"] == "in" and v["exit_at"] is None
    # A check-in recorded by mistake can be cleared too: back to pending.
    assert reg.correct_time(v["id"], "entry", None, "checked in the wrong visitor", ADMIN, now=now)["status"] == "pending"


def test_cancel_only_before_check_in(vr):
    v = register()
    assert reg.cancel_visit(v["id"], ADMIN)["status"] == "cancelled"
    assert reg.list_visits() == []      # cancelled visits are left out by default
    v2 = register(visitor_id=v["visitor_id"])
    reg.record_time(v2["id"], "entry", principal=ADMIN)
    with pytest.raises(reg.RegistryError):
        reg.cancel_visit(v2["id"], ADMIN)


# ---------------------------------------------------------------- camera detections

def _footfall_entry(camera_id, ts, employee_id=None, clip_path=None):
    with reid_db.get_connection() as conn:
        cur = conn.execute("INSERT INTO footfall_clips (person_id, day, camera_id, ts, employee_id, clip_path, clip_status) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (int(ts), datetime.date.fromtimestamp(ts).isoformat(), camera_id, ts, employee_id, clip_path,
                            "ready" if clip_path else "failed"))
        return cur.lastrowid


def test_camera_detection_becomes_entry_only_when_operator_links_it(vr):
    gate = camera_db.add_camera("Gate", "HQ")
    now = time.time()
    v = register(gate_camera_id=gate, now=now - 600)
    ff = _footfall_entry(gate, now - 300)
    _footfall_entry(gate, now - 200, employee_id="E2")          # an employee: never offered
    exit_id = reid_db.log_event(gate, 7, reid_db.EVENT_EXIT, None, None, now - 60)

    assert reg.get_visit(v["id"])["status"] == "pending"        # detection alone changes nothing
    cand = reg.candidates(v["id"], now=now)
    assert cand["kind"] == "entry" and [d["ref"] for d in cand["detections"]] == [f"ff:{ff}"]

    v = reg.link_detection(v["id"], f"ff:{ff}", ADMIN, now=now)
    assert v["status"] == "in" and v["entry_at"] == pytest.approx(now - 300)
    assert v["entry_source"] == "camera" and v["entry_camera_id"] == gate

    # The same detection cannot be a second visitor's entry.
    other = register("Meena", mobile="9000000001", now=now - 500)
    with pytest.raises(reg.RegistryError, match="already linked"):
        reg.link_detection(other["id"], f"ff:{ff}", ADMIN, now=now)

    cand = reg.candidates(v["id"], now=now)
    assert cand["kind"] == "exit" and cand["detections"][0]["ref"] == f"fx:{exit_id}"
    v = reg.link_detection(v["id"], f"fx:{exit_id}", ADMIN, now=now)
    assert v["status"] == "out" and v["duration_seconds"] == pytest.approx(240)
    day = [d for d in reg.detections(datetime.date.fromtimestamp(now).isoformat()) if d["ref"] == f"ff:{ff}"][0]
    assert day["linked"]["visit_id"] == v["id"]


def test_visitor_area_arrival_can_be_linked(vr):
    cam = camera_db.add_camera("Lobby", "HQ")
    now = time.time()
    with visitors._conn() as conn:
        conn.execute("INSERT INTO visitor_report_log (event_id, camera_id, camera_name, visit_key, alert_seq, entered_at, alert_at, left_at, last_seen) "
                     "VALUES (1, ?, 'Lobby', 'k1', 1, ?, ?, ?, ?)", (cam, now - 900, now - 890, now - 100, now - 100))
    v = register(now=now - 1000)
    refs = {d["ref"] for d in reg.detections(datetime.date.fromtimestamp(now).isoformat())}
    assert {"va:k1", "vx:k1"} <= refs
    reg.link_detection(v["id"], "va:k1", ADMIN, now=now)
    v = reg.link_detection(v["id"], "vx:k1", ADMIN, now=now)
    assert v["duration_seconds"] == pytest.approx(800)


# ---------------------------------------------------------------- snapshots and privacy

def _frame(face_box=None, sharp=True):
    img = np.full((480, 640, 3), 120, np.uint8)
    if face_box:
        x1, y1, x2, y2 = face_box
        patch = np.random.default_rng(1).integers(40, 220, (y2 - y1, x2 - x1, 3), dtype=np.uint8) if sharp else np.full((y2 - y1, x2 - x1, 3), 128, np.uint8)
        img[y1:y2, x1:x2] = patch
    return img


def test_no_snapshot_without_consent(vr, monkeypatch):
    cam = camera_db.add_camera("Reception", "HQ")
    v = register(gate_camera_id=cam)
    monkeypatch.setattr("app.camera_stream.grab_still", lambda *a, **k: pytest.fail("must not touch the camera"))
    with pytest.raises(reg.RegistryError, match="did not agree"):
        reg.capture_live(v["id"], cam, ADMIN)
    assert reg._save_best_snapshot(v["id"], [_frame((300, 100, 400, 200))], time.time(), cam, "x", "live")["saved"] is False


def test_best_clear_face_is_kept_once(vr, monkeypatch):
    cam = camera_db.add_camera("Reception", "HQ")
    v = register(gate_camera_id=cam, snapshot_consent=True)
    boxes = {}
    monkeypatch.setattr(reg, "detect_faces", lambda img: boxes.get(id(img), []))
    small = _frame((10, 10, 30, 30))                 # too small
    blurred = _frame((300, 100, 400, 200), sharp=False)
    good = _frame((300, 100, 380, 180))
    better = _frame((280, 80, 400, 200))
    boxes.update({id(small): [(10, 10, 30, 30, 0.9)], id(blurred): [(300, 100, 400, 200, 0.9)],
                  id(good): [(300, 100, 380, 180, 0.8)], id(better): [(280, 80, 400, 200, 0.95)]})

    r = reg._save_best_snapshot(v["id"], [small, blurred], time.time(), cam, "live_capture", "live")
    assert r["saved"] is False and "No clear" in r["reason"]
    assert reg.get_visit(v["id"])["snapshot_status"] == "no_clear_face"

    assert reg._save_best_snapshot(v["id"], [good], time.time(), cam, "live_capture", "live")["saved"]
    assert reg._save_best_snapshot(v["id"], [good], time.time(), cam, "live_capture", "live")["saved"] is False  # no duplicate
    assert reg._save_best_snapshot(v["id"], [better], time.time(), cam, "live_capture", "live")["saved"]
    media = reg.get_visit(v["id"], with_detail=True)["media"]
    assert [m["kind"] for m in media] == ["snapshot"]        # the clearer one replaced the first
    path, ctype = reg.media_file(media[0]["id"])
    assert ctype == "image/jpeg" and path.is_file() and len(list(path.parent.iterdir())) == 1
    assert reg.get_visit(v["id"])["snapshot_status"] == "captured"


def test_no_detector_is_reported_not_faked(vr, monkeypatch):
    v = register(snapshot_consent=True)
    monkeypatch.setattr(reg, "detect_faces", lambda img: None)
    r = reg._save_best_snapshot(v["id"], [_frame()], time.time(), None, "live_capture", "live")
    assert r["saved"] is False and "not available" in r["reason"]
    assert reg.get_visit(v["id"])["snapshot_status"] == "unavailable"


def test_linked_clip_is_kept_and_snapshotted_only_with_consent(vr, monkeypatch, tmp_path):
    gate = camera_db.add_camera("Gate", "HQ")
    clip = tmp_path / "footfall_clips" / str(gate) / "c.mp4"
    clip.parent.mkdir(parents=True)
    from app import clips
    clips.encode_mp4(clip, [cv2.imencode(".jpg", _frame((280, 80, 400, 200)))[1].tobytes()] * 6, 6)
    monkeypatch.setattr("app.footfall.FootfallService.clip_file", staticmethod(lambda cid: (clip, "ok")))
    monkeypatch.setattr(reg, "detect_faces", lambda img: [(280, 80, 400, 200, 0.95)])
    ran = []
    monkeypatch.setattr(reg.threading, "Thread", lambda target, args, **k: type("T", (), {"start": lambda s: (ran.append(1), target(*args))})())
    now = time.time()
    yes = register(gate_camera_id=gate, snapshot_consent=True, now=now - 600)
    no = register("Meena", mobile="9000000001", gate_camera_id=gate, now=now - 600)
    reg.link_detection(yes["id"], f"ff:{_footfall_entry(gate, now - 300, clip_path='x')}", ADMIN, now=now)
    reg.link_detection(no["id"], f"ff:{_footfall_entry(gate, now - 200, clip_path='y')}", ADMIN, now=now)
    assert ran == [1]                                                  # only the consenting visit
    kinds = sorted(m["kind"] for m in reg.get_visit(yes["id"], with_detail=True)["media"])
    assert kinds == ["clip", "snapshot"]
    assert reg.get_visit(no["id"], with_detail=True)["media"] == []


def test_retention_and_erasure(vr, monkeypatch):
    v = register(snapshot_consent=True)
    monkeypatch.setattr(reg, "detect_faces", lambda img: [(280, 80, 400, 200, 0.95)])
    reg._save_best_snapshot(v["id"], [_frame((280, 80, 400, 200))], time.time() - 10 * 86400, None, "live_capture", "live")
    mid = reg.get_visit(v["id"], with_detail=True)["media"][0]["id"]
    path, _ = reg.media_file(mid)
    reg.set_settings({"media_retention_days": 7}, ADMIN)              # older than 7 days -> removed now
    assert reg.media_file(mid) == (None, None) and not path.exists()
    assert reg.get_visit(v["id"])["snapshot_status"] == "expired"
    assert reg.get_visit(v["id"])["status"] == "pending"              # the visit record itself is kept
    with pytest.raises(reg.RegistryError):
        reg.set_settings({"media_retention_days": 0}, ADMIN)

    reg._save_best_snapshot(v["id"], [_frame((280, 80, 400, 200))], time.time(), None, "live_capture", "live")
    path, _ = reg.media_file(reg.get_visit(v["id"], with_detail=True)["media"][0]["id"])
    assert reg.delete_visitor(v["visitor_id"], ADMIN)
    assert not path.exists() and reg.search_visitors() == []
    with pytest.raises(KeyError):
        reg.get_visit(v["id"])


# ---------------------------------------------------------------- KPIs, analytics, export

def test_kpis_and_monthly_history(vr):
    day = datetime.date.today().isoformat()
    now = time.time()
    a = register("Anil", mobile="9000000002", visit_date=day, now=now - 7200)
    reg.record_time(a["id"], "entry", now - 7000, ADMIN, now=now)
    reg.record_time(a["id"], "meeting_start", now - 6400, ADMIN, now=now)
    reg.record_time(a["id"], "exit", now - 3400, ADMIN, now=now)          # 60 min stay, 10 min wait
    b = register("Bina", mobile="9000000003", visit_date=day, now=now - 7200)
    reg.record_time(b["id"], "entry", now - 7300, ADMIN, now=now)        # still inside for over 2 h -> long stay
    register("Chetan", mobile="9000000004", visit_date=day, now=now)     # pending
    again = register(visitor_id=a["visitor_id"], visit_date=day, now=now)  # Anil again today: still first-time today
    yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    old = register("Dev", mobile="9000000006", visit_date=yesterday, now=now - 86400)
    register(visitor_id=old["visitor_id"], visit_date=day, now=now)     # visited before -> repeat today

    k = reg.kpis(day, now=now)
    assert k["visitors_today"] == 4 and k["first_time_today"] == 3 and k["repeat_today"] == 1
    assert k["inside_now"] == 1 and k["checked_out_today"] == 1 and k["pending_today"] == 3
    assert k["average_duration_seconds"] == 3600 and k["average_waiting_seconds"] == 600 and k["waiting_measured"] == 1
    assert k["long_staying"] == 1 and k["long_staying_names"] == ["Bina"]
    same_month = yesterday[:7] == day[:7]
    assert k["visits_this_month"] == (6 if same_month else 5)
    assert k["repeat_visits_this_month"] == 2
    assert k["most_frequent"][0]["name"] == "Anil" and k["most_frequent"][0]["visits"] == 2

    rows = reg.list_visits(date_from=day, date_to=day, now=now)
    assert len(rows) == 5
    repeat_codes = {r["visit_code"] for r in reg.list_visits(visitor_type="repeat", now=now)}
    assert again["visit_code"] in repeat_codes and len(repeat_codes) == 2
    month_of = {"Anil": 2, "Dev": 2 if same_month else 1}
    assert all(r["visits_this_month"] == month_of.get(r["visitor_name"], 1) for r in rows)
    assert len(reg.list_visits(status="pending", now=now)) == 4          # incl. Dev's visit yesterday
    assert len(reg.list_visits(date_from=day, department="Sales", now=now)) == 5
    assert [r["visitor_name"] for r in reg.list_visits(q="bin", now=now)] == ["Bina"]
    p = reg.get_profile(a["visitor_id"], now=now)
    assert p["total_visits"] == 2 and p["visits_this_month"] == 2 and p["first_visit"] == day


def test_analytics_counts_real_records(vr):
    now = time.time()
    a = register("Anil", mobile="9000000002", visit_date="2026-09-01", purpose="Interview")
    register(visitor_id=a["visitor_id"], visit_date="2026-09-08", purpose="interview")
    register("Bina", mobile="9000000003", visit_date="2026-09-08", purpose="Delivery", host_employee_id="E2")
    r = reg.analytics("2026-09-01", "2026-09-30", now=now)
    assert r["total_visits"] == 3 and r["unique_visitors"] == 2 and r["repeat_visitors"] == 1
    assert len(r["daily"]) == 30 and sum(d["visits"] for d in r["daily"]) == 3
    assert {w["week"]: w["visits"] for w in r["weekly"]} == {"2026-08-31": 1, "2026-09-07": 2}
    assert r["monthly"] == [{"month": "2026-09", "visits": 3}]
    assert r["by_purpose"][0] == {"label": "Interview", "visits": 2}
    assert {d["label"] for d in r["by_department"]} == {"Sales", "Accounts"}
    assert r["frequency"][:2] == [{"label": "1 visit", "visitors": 1}, {"label": "2 visits", "visitors": 1}]
    assert r["average_waiting_seconds"] is None                          # nothing measured, nothing invented
    with pytest.raises(reg.RegistryError):
        reg.analytics("2026-09-30", "2026-09-01")


def test_exports(vr):
    register("=cmd|' /C calc'!A0", mobile="9000000005", visit_date="2026-09-01")
    header, rows = reg.export_rows(date_from="2026-09-01", date_to="2026-09-30")
    csv_text = reg.to_csv(header, rows).decode("utf-8-sig")
    assert csv_text.splitlines()[0].startswith("Visit ID,Visitor ID,Visitor name")
    assert "'=cmd" in csv_text                                           # no formula injection
    x = zipfile.ZipFile(io.BytesIO(reg.to_xlsx(header, rows)))
    assert "xl/worksheets/sheet1.xml" in x.namelist() and "Person to meet" in x.read("xl/worksheets/sheet1.xml").decode()


# ---------------------------------------------------------------- API

def test_api_flow(api, admin_token, vr):
    h = bearer(admin_token)
    from app import face_db
    face_db.upsert_employee("E1", "Asha")
    assert api.get("/api/visitor-records/kpis").status_code == 401
    hosts = api.get("/api/visitor-records/hosts", headers=h).json()["hosts"]
    assert hosts[0]["employee_id"] == "E1"
    body = {"name": "Ravi", "purpose": "Meeting", "host_employee_id": "E1", "mobile": "9876543210"}
    r = api.post("/api/visitor-records/visits", json=body, headers=h)
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    dup = api.post("/api/visitor-records/visits", json=body, headers=h)
    assert dup.status_code == 409 and dup.json()["matches"][0]["visitor_code"] == r.json()["visitor_code"]
    assert api.post(f"/api/visitor-records/visits/{vid}/times", json={"kind": "entry"}, headers=h).json()["status"] == "in"
    bad = api.post(f"/api/visitor-records/visits/{vid}/correct", json={"kind": "exit", "ts": 1, "note": "x"}, headers=h)
    assert bad.status_code == 422
    assert api.get(f"/api/visitor-records/visits/{vid}", headers=h).json()["events"][0]["kind"] == "registered"
    assert api.get("/api/visitor-records/visits?status=in", headers=h).json()["visits"][0]["id"] == vid
    assert api.get("/api/visitor-records/kpis", headers=h).json()["inside_now"] == 1
    assert api.get("/api/visitor-records/export?format=xlsx", headers=h).headers["content-type"].startswith("application/vnd.openxml")
    assert api.get("/api/visitor-records/visits/999", headers=h).status_code == 404
    assert api.post(f"/api/visitor-records/visits/{vid}/snapshot", json={}, headers=h).status_code == 422   # no consent


def test_live_capture_runs_in_background_and_reports_outcome(vr, monkeypatch):
    cam = camera_db.add_camera("Reception", "HQ")
    v = register(gate_camera_id=cam, snapshot_consent=True)
    jpg = cv2.imencode(".jpg", _frame((280, 80, 400, 200)))[1].tobytes()
    grabs = []
    monkeypatch.setattr("app.camera_stream.grab_still", lambda cid, timeout=0: grabs.append(cid) or jpg)
    monkeypatch.setattr(reg, "detect_faces", lambda img: [(280, 80, 400, 200, 0.95)])
    started = []
    monkeypatch.setattr(reg.threading, "Thread", lambda target, args, **k: type("T", (), {"start": lambda s: started.append((target, args))})())

    r = reg.capture_live(v["id"], None, ADMIN)                     # the visit's gate camera
    assert r["started"] and r["frames_checked"] == reg.LIVE_CAPTURE_FRAMES and set(grabs) == {cam}
    assert r["visit"]["snapshot_status"] == "capturing"
    with pytest.raises(reg.RegistryError, match="already being taken"):
        reg.capture_live(v["id"], None, ADMIN)
    target, args = started[0]
    target(*args)                                                   # the background job
    v = reg.get_visit(v["id"])
    assert v["snapshot_status"] == "captured" and v["snapshot_id"] and "Saved" in v["snapshot_message"]

    # A later attempt with no clear face keeps the snapshot already saved.
    monkeypatch.setattr(reg, "detect_faces", lambda img: [])
    reg.capture_live(v["id"], None, ADMIN)
    target, args = started[1]
    target(*args)
    v = reg.get_visit(v["id"])
    assert v["snapshot_status"] == "captured" and "No clear" in v["snapshot_message"]


@pytest.mark.parametrize("day", ["2026-11-15", "2026-02-10", "2028-02-29", "2026-12-31"])
def test_kpis_work_in_months_shorter_than_31_days(vr, day):
    register(visit_date=day)
    k = reg.kpis(day, now=at(day, 18))
    assert k["visitors_today"] == 1 and k["visits_this_month"] == 1
