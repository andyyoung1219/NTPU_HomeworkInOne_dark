import hashlib
import io
import json
import zipfile
from datetime import datetime, timedelta

import pytest
from docx import Document
from fastapi.testclient import TestClient
from test_mvp import FakeAI, await_task, task_request
from test_mvp import service as service

from backend.main import ORIGIN, create_app
from backend.services.exports import export_extra, render_docx
from backend.services.learning import TAIPEI
from backend.services.study import StudyError, StudyService
from frontend.gradio_app import build_ui


def second_course(service):
    service.store.save_many(service.user, [
        ("course", "3", {"id": "3", "name": "另一門課"}),
        ("assignment", "4", {"id": "4", "course_id": "3", "title": "另一門作業",
                             "description": "機密的別課筆記", "asset_ids": []}),
    ])


def completed(service):
    task = service.create_task({**task_request(service), "data_transfer_approved": True}, "p4")
    task = await_task(service, task["id"])
    return service.save_draft(task["id"], task["draft"], reviewed=True, accept_incomplete=True)


def test_dashboard_dates_unknown_status_and_course_scope(service):
    second_course(service)
    a = service.get("assignment", "2")
    a.update(due_at=datetime.now(TAIPEI).timestamp() - 100, submission_status="unknown")
    service.store.save(service.user, "assignment", "2", a)
    data = service.learning.dashboard()
    assert len(data["unknown"]) == 2
    assert data["overdue"] == []  # Unknown does not mean not submitted.
    a["submission_status"] = "draft"
    service.store.save(service.user, "assignment", "2", a)
    assert service.learning.dashboard()["overdue"][0]["id"] == "2"
    a["last_synced_at"] = (datetime.now(TAIPEI) - timedelta(days=1)).isoformat()
    service.store.save(service.user, "assignment", "2", a)
    data = service.learning.dashboard()
    assert data["overdue"] == []
    assert any(item["id"] == "2" for item in data["unknown"])
    a["last_synced_at"] = datetime.now(TAIPEI).isoformat()
    a["stale"] = True
    service.store.save(service.user, "assignment", "2", a)
    data = service.learning.dashboard()
    assert data["overdue"] == []
    assert any(item["id"] == "2" for item in data["unknown"])
    a["submission_status"] = "submitted"
    service.store.save(service.user, "assignment", "2", a)
    assert service.learning.dashboard()["overdue"] == []
    course = service.learning.course("1")
    assert [a["id"] for a in course["assignments"]] == ["2"]
    assert all(a["assignment_id"] == "2" for a in course["assets"])
    assert service.ai.calls == []


def test_search_chinese_short_queries_dates_and_user_isolation(service):
    second_course(service)
    a = service.get("assignment", "2")
    a.update(description="請分析資料探勘的分類方法。", due_at=datetime(2026, 11, 8, 23, 59, tzinfo=TAIPEI).timestamp())
    service.store.save(service.user, "assignment", "2", a)
    assert service.learning.search("資料探勘")["results"][0]["ref"] == "2"
    assert service.learning.search("分類", "1")["results"][0]["ref"] == "2"
    assert service.learning.search("分類", "3")["results"] == []
    assert service.learning.search("分類", due_to="2026-11-08")["results"]
    assert not service.learning.search("分類", due_from="2026-11-09")["results"]
    for query in ['" OR *', "' OR 1=1 --", "%", "_"]:
        assert service.learning.search(query)["results"] == []
    with pytest.raises(StudyError, match="SEARCH_DATE_INVALID"):
        service.learning.search("", due_from="2026-11-10", due_to="2026-11-01")
    service.user = "c" * 24
    assert service.learning.search("分類")["results"] == []
    assert service.learning.progress() is None
    with pytest.raises(StudyError):
        service.learning.course("1")
    assert service.ai.calls == []


def test_file_search_page_hash_invalidation_and_removed_sources(service):
    result = service.learning.search("submit a report")
    hit = next(h for h in result["results"] if h["kind"] == "asset")
    assert hit["page"] == 1 and hit["assignment_id"] == "2"
    assert hit["sha256"] == service.get("asset", "a" * 24)["sha256"]
    (service.root / "source.pdf").write_bytes(b"changed")
    result = service.learning.search("submit a report")
    assert result["results"] == []
    assert any(i["code"] == "ASSET_CHANGED" for i in result["index"]["issues"])
    a = service.get("assignment", "2")
    a["removed"] = True
    service.store.save(service.user, "assignment", "2", a)
    assert not service.learning.search("sample.pdf")["results"]


def test_material_search_only_current_authorized_contents(service):
    data = "這份教材包含機器學習分類方法。".encode()
    (service.root / "material.txt").write_bytes(data)
    material = {"id": "m", "course_id": "1", "name": "教材.txt", "path": "material.txt",
                "mime_type": "text/plain", "sha256": hashlib.sha256(data).hexdigest()}
    service.store.save(service.user, "material", "m", material)
    assert not service.learning.search("機器學習")["results"]
    service.hub.save_snapshot("moodle:1:contents", [{"name": "第一週", "modules": [
        {"name": "教材", "contents": [material]}]}], "moodle")
    hit = service.learning.search("機器學習")["results"][0]
    assert hit["section"] == 1 and hit["kind"] == "material"
    service.hub.save_snapshot("moodle:1:contents", [], "moodle")
    assert not service.learning.search("機器學習")["results"]


def test_progress_restart_preserves_edits_and_clears_approval(service):
    task = completed(service)
    value = {"course_id": "1", "assignment_id": "2", "selected_asset_ids": ["a" * 24],
             "mode": "code_solution", "instruction": "請保留修改", "excerpt": "自訂節錄",
             "requirements": "自訂要求", "task_id": task["id"], "editor": "# 尚未按保存的修改",
             "research_query": "10.1234/example", "research_provider": "europepmc", "research_full_text": True,
             "page": "review", "reviewed": True, "data_transfer_approved": True}
    service.learning.save_progress(value)
    restarted = StudyService(service.root, FakeAI())
    try:
        restored = restarted.learning.progress()
        assert restored["editor"] == value["editor"]
        assert restored["excerpt"] == value["excerpt"]
        assert restored["research_query"] == value["research_query"]
        assert restored["research_provider"] == value["research_provider"]
        assert restored["research_full_text"] is True
        assert restored["page"] == "review"
        assert not restored["reviewed"] and not restored["data_transfer_approved"]
        assert restarted.ai.calls == []
    finally:
        restarted.close()
    second_course(service)
    with pytest.raises(StudyError, match="COURSE_SCOPE_MISMATCH"):
        service.learning.save_progress({**value, "assignment_id": "4", "selected_asset_ids": []})
    with pytest.raises(StudyError, match="COURSE_SCOPE_MISMATCH"):
        service.learning.save_progress({**value, "course_id": "3", "assignment_id": "4", "selected_asset_ids": []})


def test_interrupted_generation_restores_configuration_without_replaying(service):
    task = service.create_task(task_request(service), "interrupted")
    service.learning.save_progress({"course_id": "1", "assignment_id": "2", "task_id": task["id"],
                                    "excerpt": "請整理", "page": "generate"})
    assert service.learning.progress()["page"] == "configure"
    ui = build_ui(service)
    callback = next(e.fn for e in ui.fns.values() if e.fn and e.fn.__name__ == "restore_workspace")
    values = callback()
    assert values[20] == "configure"
    assert values[18] is False  # AI approval
    assert values[15] is False  # export approval
    assert service.ai.calls == []


def test_docx_semantics_and_zip_allowlist_sources_and_invalidation(service):
    task = completed(service)
    identifier = task["id"]
    workspace = service.workspace(service.user, identifier)
    (workspace / "secret.txt").write_text("PRIVATE_TOKEN_DO_NOT_EXPORT")
    task = export_extra(service, identifier, "docx")
    doc = Document(service.safe_path(task["exports"]["docx"]["path"]))
    assert any(p.style.name == "Title" for p in doc.paragraphs)
    assert doc.tables and doc.tables[0].cell(0, 0).text == "項目"
    assert doc.core_properties.author == ""
    task = export_extra(service, identifier, "zip")
    with zipfile.ZipFile(service.safe_path(task["exports"]["zip"]["path"])) as archive:
        assert {"report.pdf", "report.docx", "report.md", "manifest.json"} <= set(archive.namelist())
        assert not any(n.startswith("sources/") or "secret" in n for n in archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        for name, digest in manifest["artifacts"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    task = export_extra(service, identifier, "zip", include_sources=True)
    with zipfile.ZipFile(service.safe_path(task["exports"]["zip"]["path"])) as archive:
        assert "sources/assignment-description.txt" in archive.namelist()
        assert any(n.startswith("sources/") and n.endswith(".pdf") for n in archive.namelist())
    with TestClient(create_app(service, "p4"), base_url=ORIGIN) as client:
        assert client.get("/api/dashboard").status_code == 401
        client.get("/unlock/p4")
        assert client.get("/api/dashboard").status_code == 200
        url = f"/api/artifacts/{identifier}/download?kind=docx"
        assert client.get(url).status_code == 200
        service.save_draft(identifier, task["draft"] + "\n修改內容")
        assert client.get(url).status_code == 400
        assert client.post(f"/api/tasks/{identifier}/export-extra", json={"kind": "docx"}).status_code == 400


def test_exports_reject_tampered_artifacts_and_cross_user(service):
    task = completed(service)
    task = service.export(task["id"])
    service.safe_path(task["pdf"]).write_bytes(b"tampered")
    with pytest.raises(StudyError, match="ARTIFACT_CHANGED"):
        export_extra(service, task["id"], "zip")
    service.user = "c" * 24
    with pytest.raises(StudyError):
        export_extra(service, task["id"], "docx")


def test_docx_does_not_fetch_links_and_preserves_code(tmp_path):
    target = tmp_path / "report.docx"
    render_docx("# 中文報告\n\n## 方法\n- 第一點\n\n```python\nx = '<b>&'\n```\n"
                "![外部圖片](https://example.invalid/private)\n[來源](https://example.invalid)", target)
    document = Document(io.BytesIO(target.read_bytes()))
    text = "\n".join(p.text for p in document.paragraphs)
    assert "x = '<b>&'" in text
    assert "圖片待補" in text
    assert "https://example.invalid" in text


def test_course_today_uses_taipei_weekday_and_snapshot(service):
    today = datetime.now(TAIPEI)
    service.hub.save_snapshot("sis:timetable", {"courses": [
        {"name": "今日課程", "teacher": "老師", "meetings": [
            {"weekday": today.isoweekday(), "period": 2, "classroom": "201"}]},
        {"name": "明日課程", "meetings": [{"weekday": (today + timedelta(days=1)).isoweekday(), "period": 1}]},
    ]}, "sis")
    assert [c["name"] for c in service.learning.dashboard()["today"]] == ["今日課程"]


def test_ui_history_restores_own_course_and_latest_draft(service):
    task = completed(service)
    second_course(service)
    ui = build_ui(service)
    callbacks = {e.fn.__name__: e.fn for e in ui.fns.values() if e.fn}
    values = callbacks["open_saved_task"](task["id"])
    assert values[0]["value"] == "1" and values[1]["value"] == "2"
    assert values[11] == task["id"] and values[13] == task["draft"]
    assert values[20] == "review"
    assert values[15] is False and values[18] is False
    latest = callbacks["open_latest_task"]()
    assert latest[11] == task["id"] and latest[13] == task["draft"]
    other = callbacks["open_from_dashboard"]("4")
    assert other[0]["value"] == "3" and other[1]["value"] == "4"
    assert other[11] == "" and other[13] == ""
    assert task["draft"] not in other[5]
