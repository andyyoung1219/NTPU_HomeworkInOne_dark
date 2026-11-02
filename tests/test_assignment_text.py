import concurrent.futures
import hashlib
import json

import pytest
from fastapi.testclient import TestClient
from test_mvp import FakeAI, await_task

from backend.main import ORIGIN, create_app
from backend.services.documents import assignment_text
from backend.services.study import StudyError, StudyService


@pytest.fixture
def text_service(tmp_path):
    ai = FakeAI()

    def begin(prompt, asset_ref=None):
        ai.calls.append(prompt)
        future = concurrent.futures.Future()
        future.set_result(json.dumps({"title": "純文字作業草稿", "sections": [
            {"heading": "題目要求", "content": "請比較兩種方法並提供引用。"}],
            "sources": ["assignment-description"], "unresolved": ["補上本人分析"]}))
        return "text-only-model", future

    ai.begin = begin
    service = StudyService(tmp_path, ai)
    service.user = "d" * 24
    service.store.save_many(service.user, [
        ("course", "1", {"id": "1", "name": "Synthetic course"}),
        ("assignment", "2", {"id": "2", "course_id": "1", "title": "Text-only assignment",
                               "description": "請比較兩種方法並提供引用。", "asset_ids": []}),
    ])
    yield service
    service.close()


def request_for(service):
    parsed = service.parse_assignment("2", [])
    return {"assignment_id": "2", "mode": "summary", "excerpt": parsed["excerpt"]}


def test_normalize_all_instructor_fields_without_student_feedback():
    result = assignment_text({"intro": "<p>請比較兩種方法。</p><p>須提供引用。</p>",
                              "activity": "<p>請繳交 PDF。</p>"},
                             submission={"assignmentdata": {"activity": "<p>請繳交 PDF。</p>"},
                                         "feedback": {"text": "PRIVATE_GRADE"}})
    assert len(result["description_parts"]) == 2
    assert "請比較兩種方法。\n\n須提供引用。" in result["description"]
    assert result["description"].count("請繳交 PDF。") == 1
    assert "PRIVATE_GRADE" not in result["description"]
    assert result["description_status"] == "available"


def test_sync_falls_back_to_matching_course_module(text_service, monkeypatch):
    service = text_service
    monkeypatch.setattr(service, "verify_identity", lambda: None)
    service.moodle.courses = {1: {}}

    def assignments(_):
        service.moodle.assignments = {"2": {"id": 2, "cmid": 3, "name": "Text only", "intro": ""}}
        service.moodle.assets = {}

    monkeypatch.setattr(service.moodle, "list_assignments", assignments)
    monkeypatch.setattr(service.moodle, "credentials", lambda: ("synthetic", {"core_course_get_contents", "mod_assign_get_submission_status"}))

    def call(function, token, params):
        if function == "core_course_get_contents":
            assert params["courseid"] == 1
            return 200, [{"modules": [
                {"modname": "assign", "id": 3, "instance": 2, "description": "<p>請完成網頁題目。</p>"},
                {"modname": "assign", "id": 99, "instance": 99, "description": "OTHER_ASSIGNMENT"},
            ]}]
        assert function == "mod_assign_get_submission_status"
        return 200, {"lastattempt": {"submission": {"status": "new"}},
                     "assignmentdata": {"activity": "<p>須說明解題步驟。</p>"},
                     "feedback": {"grade": "PRIVATE_GRADE"}}

    monkeypatch.setattr(service.moodle.client, "call", call)
    synced = service.sync_assignments("1")[0]
    assert "請完成網頁題目。" in synced["description"]
    assert "須說明解題步驟。" in synced["description"]
    assert "OTHER_ASSIGNMENT" not in synced["description"]
    assert "PRIVATE_GRADE" not in synced["description"]
    assert synced["description_status"] == "available"


def test_text_only_task_approval_and_source_snapshot(text_service):
    service = text_service
    request = request_for(service)
    task = service.create_task(request, "text-only")
    assert task["status"] == "waiting_approval" and service.ai.calls == []
    assert task["request"]["selected_asset_ids"] == []
    path = service.workspace(service.user, task["id"]) / "input/assignment-description.txt"
    assert path.read_text(encoding="utf-8") == service.get("assignment", "2")["description"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == task["source_hashes"]["assignment-description"]
    service.approve(task["id"], task["request_sha256"])
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review"
    assert "assignment-description" in service.ai.calls[0]
    assert "請比較兩種方法並提供引用。" in service.ai.calls[0]


def test_changed_description_invalidates_previous_approval(text_service):
    service = text_service
    task = service.create_task(request_for(service), "changed-description")
    assignment = service.get("assignment", "2")
    assignment["description"] = "老師更新的要求"
    service.store.save(service.user, "assignment", "2", assignment)
    with pytest.raises(StudyError, match="ASSET_CHANGED"):
        service.approve(task["id"], task["request_sha256"])
    assert service.ai.calls == []


def test_api_text_only_parse_and_task_without_asset_ids(text_service):
    with TestClient(create_app(text_service, key="text-only-session"), base_url=ORIGIN) as client:
        client.get("/unlock/text-only-session")
        parsed = client.post("/api/assignments/2/parse", json={})
        assert parsed.status_code == 200
        assert parsed.json()["data"]["assets"] == []
        response = client.post("/api/tasks", json={"assignment_id": "2", "excerpt": parsed.json()["data"]["excerpt"]},
                               headers={"Idempotency-Key": "text-api"})
        assert response.status_code == 202
        assert response.json()["data"]["status"] == "waiting_approval"
        assert text_service.ai.calls == []


def test_empty_assignment_cannot_be_misreported_as_read(text_service):
    assignment = text_service.get("assignment", "2")
    assignment["description"] = ""
    text_service.store.save(text_service.user, "assignment", "2", assignment)
    with pytest.raises(StudyError, match="ASSIGNMENT_TEXT_NOT_AVAILABLE"):
        text_service.parse_assignment("2", [])
    assert assignment_text({"intro": "<p><br></p>"})["description_status"] == "not_returned"
