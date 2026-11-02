import concurrent.futures
import hashlib
import io
import json
import time

import pymupdf
import pytest
from docx import Document
from fastapi.testclient import TestClient

from backend.main import ORIGIN, create_app
from backend.services.documents import parse, plain_html
from backend.services.reports import validate_pdf
from backend.services.study import StudyError, StudyService


class FakeAI:
    def __init__(self):
        self.calls = []

    def rpc(self, method, **params):
        return {"ready": True}

    def begin(self, prompt, asset_ref=None):
        self.calls.append(prompt)
        future = concurrent.futures.Future()
        future.set_result(json.dumps({"title": "繁體中文測試報告", "sections": [
            {"heading": "需求整理", "content": "這是合成測試資料，不是真實實驗結果。\n\n| 項目 | 狀態 |\n| --- | --- |\n| 資料 | 待補 |\n| 方法 | 待確認 |"}
        ], "sources": ["a" * 24], "unresolved": ["需由使用者執行實驗"]}, ensure_ascii=False))
        return "fake-model-id", future

    def close(self):
        pass


@pytest.fixture
def service(tmp_path):
    service = StudyService(tmp_path, FakeAI())
    service.user = "b" * 24
    (tmp_path / "active-user.json").write_text(json.dumps({"user_id": service.user}))
    asset_id = "a" * 24
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((60, 70), "Please submit a report with sources. This is a fixture.")
        data = document.tobytes()
    source = tmp_path / "source.pdf"
    source.write_bytes(data)
    service.store.save_many(service.user, [
        ("course", "1", {"id": "1", "name": "Fixture course"}),
        ("assignment", "2", {"id": "2", "course_id": "1", "title": "Fixture assignment",
                               "description": "請閱讀題目並產生草稿。", "asset_ids": [asset_id]}),
        ("asset", asset_id, {"id": asset_id, "assignment_id": "2", "name": "sample.pdf",
                            "mime_type": "application/pdf", "path": "source.pdf", "status": "downloaded",
                            "sha256": hashlib.sha256(data).hexdigest()})
    ])
    yield service
    service.close()


def task_request(service):
    parsed = service.parse_assignment("2", ["a" * 24])
    return {"assignment_id": "2", "selected_asset_ids": ["a" * 24], "excerpt": parsed["excerpt"],
            "instruction": "整理題目要求", "mode": "report_draft", "data_transfer_approved": False}


def await_task(service, task_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = service.get("task", task_id)
        if task["status"] != "running":
            return task
        time.sleep(0.01)
    pytest.fail("Task did not finish")


def test_approval_idempotency_and_snapshot_hash(service):
    request = task_request(service)
    task = service.create_task(request, "same-request")
    assert task["status"] == "waiting_approval"
    assert service.ai.calls == []
    assert service.create_task(request, "same-request")["id"] == task["id"]
    with pytest.raises(StudyError, match="IDEMPOTENCY_CONFLICT"):
        service.create_task({**request, "instruction": "different"}, "same-request")
    with pytest.raises(StudyError, match="APPROVAL_SNAPSHOT_MISMATCH"):
        service.approve(task["id"], "old-snapshot")
    service.approve(task["id"], task["request_sha256"])
    assert await_task(service, task["id"])["status"] == "waiting_review"
    assert len(service.ai.calls) == 1


def test_changed_asset_invalidates_approval(service):
    task = service.create_task(task_request(service), "changed-asset")
    (service.root / "source.pdf").write_bytes(b"changed")
    with pytest.raises(StudyError, match="ASSET_CHANGED"):
        service.approve(task["id"], task["request_sha256"])
    assert service.ai.calls == []


def test_other_user_asset_and_execution_are_rejected(service):
    request = task_request(service)
    with pytest.raises(StudyError, match="ASSET_SCOPE_MISMATCH"):
        service.create_task({**request, "selected_asset_ids": ["other"]}, "other")
    with pytest.raises(StudyError, match="MVP_TOOL_NOT_ALLOWED"):
        service.create_task({**request, "allow_code_execution": True}, "code")
    service.user = "c" * 24
    with pytest.raises(StudyError, match="NOT_FOUND_OR_NOT_AUTHORIZED"):
        service.get("asset", "a" * 24)


def test_malformed_response_is_preserved_as_partial(service):
    def invalid(prompt):
        result = concurrent.futures.Future()
        result.set_result("This is a partial draft without JSON.")
        return "bad", result
    service.ai.begin = invalid
    request = task_request(service)
    task = service.create_task({**request, "data_transfer_approved": True}, "partial")
    finished = await_task(service, task["id"])
    assert finished["status"] == "partial"
    assert "partial draft" in finished["draft"]
    assert (service.workspace(service.user, task["id"]) / "work/report.md").exists()


def test_cjk_pdf_requires_review_and_survives_restart(service):
    request = task_request(service)
    task = service.create_task({**request, "data_transfer_approved": True}, "pdf")
    task = await_task(service, task["id"])
    with pytest.raises(StudyError, match="REVIEW_REQUIRED"):
        service.export(task["id"])
    service.save_draft(task["id"], task["draft"], reviewed=True)
    finished = service.export(task["id"])
    assert finished["status"] == "completed"
    path = service.safe_path(finished["pdf"])
    assert validate_pdf(path)["passed"]
    with pymupdf.open(path) as pdf:
        text = "".join(page.get_text() for page in pdf)
        assert "繁體中文測試報告" in text and "待補" in text and "第 1 頁" in text
    reopened = StudyService(service.root, FakeAI())
    assert reopened.get("task", task["id"])["draft"] == task["draft"]
    reopened.close()


def test_api_auth_validation_download_and_task_flow(service):
    app = create_app(service, key="synthetic-test-session")
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/api/courses").status_code == 401
        client.get("/unlock/synthetic-test-session")
        invalid = client.post("/api/connections/moodle/start", json={"username": "fixture", "password": {"raw": "DO_NOT_ECHO"}})
        assert invalid.status_code == 422 and "DO_NOT_ECHO" not in invalid.text
        assert client.post("/api/assignments/2/download", json={"selected_asset_ids": ["a" * 24]}).status_code == 400
        request = task_request(service)
        created = client.post("/api/tasks", json=request, headers={"Idempotency-Key": "api-test"})
        assert created.status_code == 202
        task = created.json()["data"]
        assert service.ai.calls == []
        approved = client.post(f"/api/tasks/{task['id']}/approve", json={"request_sha256": task["request_sha256"]})
        assert approved.status_code == 202
        await_task(service, task["id"])
        assert client.get(f"/api/tasks/{task['id']}/events").status_code == 200
        assert client.get(f"/api/artifacts/{task['id']}/download?kind=markdown").status_code == 200
        assert client.get("/api/assets/other/preview").status_code == 404
        assert client.post("/api/sync/moodle", headers={"origin": "https://evil.example"}).status_code == 403


def test_docx_tables_csv_preview_and_html_injection():
    document = Document()
    document.add_heading("題目", 0)
    document.add_paragraph("請比較兩種方法")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "方法"
    table.cell(0, 1).text = "結果"
    stream = io.BytesIO()
    document.save(stream)
    chunks, flags = parse(stream.getvalue(), ".docx", "application/octet-stream", "fixture")
    assert any("方法 | 結果" in chunk["text"] for chunk in chunks)
    rows = "name,value\n" + "\n".join(f"row{i},{i}" for i in range(100))
    csv_chunks, flags = parse(rows.encode(), ".csv", "text/csv", "csv")
    assert "CSV_PREVIEW_ONLY" in flags and "row99" not in csv_chunks[0]["text"]
    assert plain_html("<script>steal()</script><p>題目</p>") == "題目"


def test_sync_failure_marks_cache_stale_without_destroying_it(service, monkeypatch):
    service.store.save(service.user, "assignment", "2", {**service.get("assignment", "2"), "stale": False})
    def fail():
        raise StudyError("NETWORK_DOWN")
    monkeypatch.setattr(service, "verify_identity", fail)
    with pytest.raises(StudyError, match="NETWORK_DOWN"):
        service.sync_assignments("1")
    assert service.get("assignment", "2")["title"] == "Fixture assignment"
    assert service.get("assignment", "2")["stale"] is True
