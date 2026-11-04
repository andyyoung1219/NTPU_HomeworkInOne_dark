import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from test_mvp import FakeAI, await_task
from test_sis import IDENTITY, enrolment, graduation_html

from backend.integrations.sis import identity_key, validate_bridge
from backend.integrations.sis_parser import parse_enrolment, parse_report
from backend.main import ORIGIN, create_app
from backend.p0.moodle import probe
from backend.services.data_hub import course_metadata
from backend.services.study import StudyError, StudyService
from frontend.data_hub import report_rows, snapshot_label

FUNCTIONS = {"core_course_get_contents", "mod_forum_get_forums_by_courses",
             "mod_forum_get_forum_discussions", "gradereport_user_get_grade_items",
             "mod_assign_get_submission_status", "core_course_get_enrolled_courses_by_timeline_classification"}


@pytest.fixture
def hub_service(tmp_path, monkeypatch):
    service = StudyService(tmp_path, FakeAI())
    service.user = "a" * 24
    monkeypatch.setattr(service, "verify_identity", lambda: None)
    monkeypatch.setattr(service.moodle, "credentials", lambda: ("synthetic-token", FUNCTIONS))
    monkeypatch.setattr(probe, "site_info", lambda *args: ({"userid": 42, "username": IDENTITY}, FUNCTIONS))
    for key in ("1", "2"):
        service.store.save(service.user, "course", key, {"id": key, "name": "合成課程" + key,
                           "semester": "115-1", "code": "U100" + key, "scope": "inprogress"})
        service.moodle.courses[int(key)] = {"id": int(key)}

    def list_assignments(cid):
        aid = str(100 + int(cid))
        service.moodle.assignments = {aid: {"id": int(aid), "name": "合成作業", "cmid": int(aid) + 10,
                                           "intro": "<p>請比較兩種方法。</p>", "teamsubmission": 0}}
        service.moodle.assets = {}

    monkeypatch.setattr(service.moodle, "list_assignments", list_assignments)

    def call(function, token, params):
        assert token == "synthetic-token"
        if function == "core_course_get_contents":
            cid = params["courseid"]
            return 200, [{"id": 1, "name": "第一週", "summary": "<p>學習重點</p>", "modules": [
                {"id": cid * 10, "name": "教材", "modname": "resource", "description": "<p>老師說明</p>",
                 "url": f"https://lms3.ntpu.edu.tw/mod/resource/view.php?id={cid*10}", "contents": [
                    {"type": "file", "filename": "test.txt", "filesize": 6, "mimetype": "text/plain", "timemodified": 100,
                     "fileurl": f"https://lms3.ntpu.edu.tw/pluginfile.php/{cid}/test.txt"},
                    {"type": "url", "filename": "外部閱讀", "fileurl": "https://example.org/reading"},
                    {"type": "url", "filename": "不安全連結", "fileurl": "javascript:alert(1)"}]}]}]
        if function == "mod_forum_get_forums_by_courses":
            cid = params["courseids[0]"]
            return 200, [{"id": cid * 20, "course": cid, "type": "news"}, {"id": 999, "course": cid, "type": "general"}]
        if function == "mod_forum_get_forum_discussions":
            assert params["forumid"] != 999
            return 200, {"discussions": [{"discussion": params["forumid"] * 10, "name": "合成公告",
                                         "message": "<p>請閱讀教材。</p>", "timemodified": 123, "attachments": []}]}
        if function == "gradereport_user_get_grade_items":
            assert params["userid"] == 42
            return 200, {"usergrades": [{"userid": 42, "courseid": params["courseid"], "gradeitems": [
                {"id": 2, "itemname": "測驗", "gradeformatted": "82.50", "rangeformatted": "0–100"},
                {"id": 3, "itemname": "未公開評分", "gradeformatted": "PRIVATE_HIDDEN_GRADE", "gradeishidden": True}]}]}
        if function == "mod_assign_get_submission_status":
            return 200, {"lastattempt": {"submission": {"status": "draft"}}}
        raise AssertionError(function)

    monkeypatch.setattr(service.moodle.client, "call", call)
    yield service
    service.close()


def test_multi_course_sync_dedup_stable_ids_and_source_separation(hub_service):
    service = hub_service
    service.hub.save_snapshot("sis:grades", {"sections": [{"grade": "A"}]}, "sis")
    first = service.hub.sync_moodle(["1", "2"])
    assert all(not s["error"] for course in first.values() for s in course.values())
    initial = service.hub.snapshot("moodle:1:contents")
    materials = service.store.list(service.user, "material")
    assert len(materials) == 2 and len({m["id"] for m in materials}) == 2
    service.hub.sync_moodle(["1", "2"])
    assert len(service.store.list(service.user, "assignment")) == 2
    assert len(service.store.list(service.user, "material")) == 2
    second = service.hub.snapshot("moodle:1:contents")
    assert initial["content_sha256"] == second["content_sha256"]
    assert initial["changed_at"] == second["changed_at"]
    assert first["1"]["grades"]["data"][0]["grade"] == "82.50"
    assert service.hub.snapshot("sis:grades")["data"]["sections"][0]["grade"] == "A"
    assert first["1"]["contents"]["data"][0]["modules"][0]["contents"][2]["url"] is None
    assert service.ai.calls == []


def test_previously_hidden_assignments_are_visible_before_and_after_sync(hub_service):
    service = hub_service
    service.sync_assignments("1")
    assignment = service.get("assignment", "101")
    service.store.save(service.user, "assignment", "101", {**assignment, "hidden": True})
    assert len(service.assignments("1")) == 1
    reopened = StudyService(service.root, FakeAI())
    reopened.user = service.user
    assert len(reopened.assignments("1")) == 1
    reopened.close()
    assert len(service.sync_assignments("1")) == 1
    assert "hidden" not in service.get("assignment", "101")


def test_removed_assignment_retained_for_history_but_hidden_from_active_list(hub_service, monkeypatch):
    service = hub_service
    service.sync_assignments("1")

    def empty(cid):
        service.moodle.assignments = {}
        service.moodle.assets = {}

    monkeypatch.setattr(service.moodle, "list_assignments", empty)
    assert service.sync_assignments("1") == []
    assert service.get("assignment", "101")["removed"]


def test_429_preserves_last_success_and_stops_all_remaining_calls(hub_service, monkeypatch):
    service = hub_service
    service.hub.sync_moodle(["1", "2"])
    before = service.hub.snapshot("moodle:1:contents")
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(args[0])
        raise probe.ProbeError("RATE_LIMITED", 429)

    monkeypatch.setattr(service.moodle.client, "call", unavailable)
    result = service.hub.sync_moodle(["1", "2"])
    assert len(calls) == 1
    assert all(s["stale"] and s["error"] == "RATE_LIMITED" for course in result.values() for s in course.values())
    after = result["1"]["contents"]
    assert after["data"] == before["data"] and after["last_synced_at"] == before["last_synced_at"]


def test_optional_permission_failure_keeps_cache_and_other_sources_update(hub_service, monkeypatch):
    service = hub_service
    service.hub.sync_moodle(["1"])
    monkeypatch.setattr(service.moodle, "credentials", lambda: ("synthetic-token", FUNCTIONS - {"gradereport_user_get_grade_items"}))
    result = service.hub.sync_moodle(["1"])["1"]
    assert result["grades"]["error"] == "MOODLE_PERMISSION_DENIED"
    assert result["grades"]["data"][0]["grade"] == "82.50"
    assert result["contents"]["error"] is None


def test_grade_scope_mismatch_never_cached(hub_service, monkeypatch):
    service = hub_service
    original = service.moodle.client.call

    def call(function, token, params):
        if function == "gradereport_user_get_grade_items":
            return 200, {"usergrades": [{"userid": 999, "courseid": 1, "gradeitems": []}]}
        return original(function, token, params)

    monkeypatch.setattr(service.moodle.client, "call", call)
    result = service.hub.sync_moodle(["1"])["1"]["grades"]
    assert result["error"] == "MOODLE_GRADE_SCOPE_MISMATCH" and result["data"] is None


def test_cache_age_and_cross_user_scope(hub_service):
    service = hub_service
    service.hub.sync_moodle(["1"])
    old = service.hub.snapshot("moodle:1:contents")
    old["last_synced_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    service.store.save(service.user, "hub", old["id"], old)
    assert service.hub.snapshot(old["id"])["stale"]
    service.user = "b" * 24
    assert service.hub.snapshot(old["id"])["data"] is None
    assert service.hub.overview()["mappings"] == []
    with pytest.raises(StudyError, match="NOT_FOUND_OR_NOT_AUTHORIZED"):
        service.hub.course("1")


def test_bridge_diagnostic_keeps_cache_and_only_saves_fixed_error_code(hub_service):
    hub = hub_service.hub
    before = hub.save_snapshot("sis:grades", {"sections": []}, "sis")
    try:
        validate_bridge({"form_url": "https://private.example/secret",
                         "formData": {"id": IDENTITY, "pw": "synthetic-secret"}}, "grades")
    except probe.ProbeError as error:
        after = hub.failed("sis:grades", error, "sis")
    assert after["error"] == "SIS_BRIDGE_LOGIN_HOST"
    assert after["data"] == before["data"]
    assert after["last_synced_at"] == before["last_synced_at"]
    assert "橋接登入主機" in snapshot_label(after)
    assert "SIS_BRIDGE_LOGIN_HOST" in snapshot_label(after)
    saved = json.dumps(hub.snapshot("sis:grades"))
    assert all(value not in saved for value in ("private.example", "secret", IDENTITY))
    recovered = hub.save_snapshot("sis:grades", {"sections": []}, "sis")
    assert recovered["error"] is None and recovered["status"] == "synced"


def test_material_download_user_scope_hash_cache_and_no_ai(hub_service, monkeypatch):
    service = hub_service
    service.hub.sync_moodle(["1"])
    ref = service.store.list(service.user, "material")[0]["id"]
    calls = []

    def download(moodle, asset, directory):
        calls.append(asset["id"])
        path = service.root / "synthetic.txt"
        path.write_bytes(b"sample")
        return {"path": path, "sha256": hashlib.sha256(b"sample").hexdigest()}

    monkeypatch.setattr("backend.services.data_hub.fetch_material", download)
    asset = service.hub.download_material(ref)
    assert service.safe_path(asset["path"]).read_bytes() == b"sample"
    assert service.hub.download_material(ref)["sha256"] == asset["sha256"] and calls == [ref]
    assert service.ai.calls == []
    service.user = "b" * 24
    with pytest.raises(probe.ProbeError, match="NOT_FOUND_OR_NOT_AUTHORIZED"):
        service.hub.download_material(ref)


def test_modified_material_invalidates_download_and_removed_ref_is_blocked(hub_service, monkeypatch):
    service = hub_service
    service.hub.sync_moodle(["1"])
    asset = service.store.list(service.user, "material")[0]
    asset.update(path="old.txt", sha256="old-hash", status="downloaded")
    service.store.save(service.user, "material", asset["id"], asset)
    changed = service.hub.file_metadata("1", "module:10", {"filename": "test.txt", "fileurl": asset["source_url"], "filesize": 6, "timemodified": 101})
    assert "path" not in changed and changed["status"] == "not_downloaded"
    service.hub.save_snapshot("moodle:1:contents", [], "moodle")
    with pytest.raises(probe.ProbeError, match="MATERIAL_NO_LONGER_LISTED"):
        service.hub.download_material(asset["id"])


def test_mapping_requires_all_fields_conflicts_explicit_and_terms_never_merge(hub_service):
    service = hub_service
    timetable = parse_enrolment(enrolment(count=1), IDENTITY)
    service.hub.save_snapshot("sis:timetable", timetable, "sis")
    course = {"id": "1", "name": "115-1 合成課程0(U1000)", "semester": "115-1", "code": "U1000"}
    service.store.save(service.user, "course", "1", course)
    assert service.hub.mappings()[0]["status"] == "matched"
    service.store.save(service.user, "course", "2", {**course, "id": "2"})
    assert service.hub.mappings()[0]["status"] == "needs_confirmation"
    service.hub.confirm_mapping("115-1:U1000", "2")
    assert service.hub.mappings()[0]["status"] == "confirmed"
    service.store.save(service.user, "course", "2", {**course, "id": "2", "semester": "114-1"})
    with pytest.raises(probe.ProbeError, match="COURSE_MAPPING_SCOPE_MISMATCH"):
        service.hub.confirm_mapping("115-1:U1000", "2")
    assert service.hub.mappings()[0]["moodle_id"] == "1"


def test_history_discovery_filters_requested_semester_and_no_assignment_fetch(hub_service, monkeypatch):
    service = hub_service
    calls = []

    def call(function, token, params):
        calls.append(function)
        assert params["classification"] == "all"
        if params["offset"]:
            return 200, {"courses": []}
        return 200, {"courses": [{"id": 5, "fullname": "114-2 合成課程", "shortname": "1142 U1234"},
                                  {"id": 6, "fullname": "115-1 舊課程"}, {"id": 7, "fullname": "學期不明"}], "nextoffset": 50}

    monkeypatch.setattr(service.moodle.client, "call", call)
    result = service.hub.discover("114-2")
    assert [c["id"] for c in result] == ["5"] and result[0]["semester"] == "114-2"
    assert set(calls) == {"core_course_get_enrolled_courses_by_timeline_classification"}
    assert service.store.get(service.user, "course", "6") is None
    assert course_metadata({"fullname": "115-1 X", "shortname": "114-2 X"})["semester"] is None


def test_sis_binding_requires_same_account_and_partial_errors_preserve_cache(hub_service, monkeypatch):
    service = hub_service
    sis = service.hub.sis
    calls = []
    monkeypatch.setattr(sis, "login", lambda *args: setattr(sis, "state", "connected"))
    data = {**parse_enrolment(enrolment(), IDENTITY), "identity_key": identity_key(IDENTITY), "source_url": "https://my.ntpu.edu.tw/"}

    def read(kind):
        calls.append(kind)
        if kind == "timetable":
            return data
        raise probe.ProbeError("SIS_REPORT_NOT_AVAILABLE")

    monkeypatch.setattr(sis, "read", read)
    with pytest.raises(probe.ProbeError, match="SIS_MOODLE_ACCOUNT_MISMATCH"):
        service.hub.connect_sis("OTHER", "synthetic-password")
    assert calls == []
    service.hub.connect_sis(IDENTITY, "synthetic-password")
    result = service.hub.sync_sis()
    assert result["timetable"]["data"]["semester"] == "115-1"
    assert result["grades"]["error"] == "SIS_REPORT_NOT_AVAILABLE"
    data = {**data, "identity_key": identity_key("OTHER")}
    result = service.hub.sync_sis()
    assert result["timetable"]["error"] == "SIS_ACCOUNT_CHANGED"
    assert result["timetable"]["data"]["identity_key"] == identity_key(IDENTITY)
    assert service.hub.sis_user is None


def test_ai_prompt_cannot_include_hub_grades_or_unselected_materials(hub_service):
    service = hub_service
    service.hub.sync_moodle(["1"])
    service.hub.save_snapshot("sis:grades", {"private": "PRIVATE_SIS_GRADE_MARKER"}, "sis")
    request = service.parse_assignment("101", [])
    task = service.create_task({"assignment_id": "101", "excerpt": request["excerpt"]}, "privacy")
    service.approve(task["id"], task["request_sha256"])
    await_task(service, task["id"])
    assert service.ai.calls
    assert "PRIVATE_SIS_GRADE_MARKER" not in service.ai.calls[0]
    assert "82.50" not in service.ai.calls[0] and "test.txt" not in service.ai.calls[0]


def test_hub_api_auth_approval_and_secret_validation(hub_service):
    service = hub_service
    with TestClient(create_app(service, key="synthetic-session"), base_url=ORIGIN) as client:
        assert client.get("/api/hub").status_code == 401
        client.get("/unlock/synthetic-session")
        assert client.get("/health").json()["phase"] == "P4"
        assert client.post("/api/sync/moodle/full", json={"course_ids": []}).status_code == 422
        assert client.post("/api/sync/moodle/full", json={"course_ids": ["1", "2"]}).status_code == 200
        assert client.get("/api/hub/courses/1").json()["data"]["grades"]["source"] == "moodle"
        assert client.post("/api/materials/unknown/download", json={"approved": False}).json()["error"]["code"] == "DOWNLOAD_APPROVAL_REQUIRED"
        assert client.put("/api/assignments/101/visibility", json={"hidden": True}).status_code == 404
        assert len(client.get("/api/courses/1/assignments").json()["data"]) == 1
        assert len(client.get("/api/courses/1/assignments?include_removed=true").json()["data"]) == 1
        response = client.post("/api/connections/sis/start", json={"username": "fixture", "password": {"raw": "SENSITIVE_VALUE"}})
        assert response.status_code == 422 and "SENSITIVE_VALUE" not in response.text
        service.user = "b" * 24
        assert client.get("/api/hub/courses/1").status_code == 404
        assert client.get("/api/hub").json()["data"]["sis"]["grades"]["data"] is None


def test_report_ui_preserves_grade_and_credit_details():
    snapshot = {"data": {"sections": [{"title": "115-1", "fields": [], "notes": [],
        "subjects": [{"title": "課程", "grade": "A", "credits": "3", "category": "必修"}]}]}}
    assert report_rows(snapshot) == [["115-1", "課程", "A", "學分：3；類別：必修"]]


def test_ambiguous_graduation_summary_keeps_other_fields_and_shows_partial_status(hub_service):
    raw = graduation_html().replace(">123<", ">123 50<")
    report = parse_report(raw, IDENTITY, "graduation")
    hub_service.hub.save_snapshot("sis:graduation", report, "sis")
    snapshot = hub_service.hub.snapshot("sis:graduation")
    assert snapshot["status"] == "partial"
    assert "已累計總學分（學分數字格式不明）" in snapshot_label(snapshot)
    rows = report_rows(snapshot)
    assert all(row[0] != "已累計總學分" for row in rows)
    assert ["系必修", "系必修", "50", "/ 60"] in rows
    assert ["校訂必修學分", "學分進度", "12.5", "28"] in rows
    with pytest.raises(probe.ProbeError, match="SIS_ACCOUNT_CHANGED"):
        parse_report(raw, "OTHER", "graduation")


def test_no_credentials_in_persisted_hub_metadata(hub_service):
    service = hub_service
    service.hub.sync_moodle(["1"])
    with service.store.connection() as db:
        payload = json.dumps([dict(row) for row in db.execute("SELECT * FROM records")])
    assert "synthetic-token" not in payload and "bridge-secret" not in payload


def test_expired_identity_before_sync_marks_all_selected_caches_stale(hub_service, monkeypatch):
    service = hub_service
    service.hub.sync_moodle(["1"])

    def expired():
        raise probe.ProbeError("invalidtoken")

    monkeypatch.setattr(service, "verify_identity", expired)
    result = service.hub.sync_moodle(["1"])["1"]
    assert all(s["stale"] and s["error"] == "invalidtoken" for s in result.values())
    assert result["grades"]["data"][0]["grade"] == "82.50"
    assert all(s["stale"] for s in service.hub.sync_sis().values())


def test_portal_login_survives_report_error_and_can_retry(hub_service, monkeypatch):
    service = hub_service
    sis = service.hub.sis
    monkeypatch.setattr(sis, "login", lambda *args: setattr(sis, "state", "connected"))

    def unavailable(kind):
        raise probe.ProbeError("SIS_SCHEMA_CHANGED")

    monkeypatch.setattr(sis, "read", unavailable)
    result = service.hub.connect_sis(" " + IDENTITY + " ", "synthetic-password")
    assert result["error"] == "SIS_SCHEMA_CHANGED"
    assert sis.state == "connected" and service.hub.sis_user == service.user
    assert service.store.get(service.user, "connection", "sis") is None
    data = {**parse_enrolment(enrolment(), IDENTITY), "identity_key": identity_key(IDENTITY),
            "source_url": "https://my.ntpu.edu.tw/"}
    monkeypatch.setattr(sis, "read", lambda kind: data)
    assert service.hub.sync_sis()["timetable"]["error"] is None
    assert service.store.get(service.user, "connection", "sis")["identity_key"] == identity_key(IDENTITY)
