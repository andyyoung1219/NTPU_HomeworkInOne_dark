"""The direct-completion deliverable is separated from app review metadata."""

import hashlib
import io
import json
import zipfile

import pymupdf
import pytest
from fastapi.testclient import TestClient
from test_mvp import service as service

from backend.main import ORIGIN, create_app
from backend.services.exports import export_extra
from backend.services.study import StudyError, StudyService
from backend.services.submission import review_markdown, submission_markdown


def report_task(mode="complete_assignment"):
    return {"title": "HW1", "course_name": "Data Mining", "request": {"mode": mode},
            "source_hashes": {"dataset": "synthetic-sha"},
            "source_names": {"dataset": "Verified dataset"}}


def report_model():
    return {"title": "Dataset Analysis", "sections": [
        {"heading": "Findings", "content": "The verified dataset contains 1,200 records."}],
        "sources": ["dataset"], "unresolved": [],
        "report_labels": {"sources_heading": "References"},
        "completion": {"status": "complete", "blockers": []}}


@pytest.mark.parametrize("heading", ["References", "參考來源", "参考文献"])
def test_completed_report_keeps_content_language_and_omits_app_checklists(heading):
    model = report_model()
    model["report_labels"]["sources_heading"] = heading
    draft, parsed = StudyService.format_result(json.dumps(model), report_task())
    assert "The verified dataset contains 1,200 records." in draft
    assert f"## {heading}\n" in draft
    assert "Verified dataset (dataset)" in draft
    assert "尚未執行" not in draft
    assert "TODO" not in draft
    assert "課程：" not in draft
    assert "待補與人工核對" not in draft
    assert parsed["completion"] == {"status": "complete", "blockers": []}


def test_legacy_blockers_and_unresolved_are_preserved_in_separate_checklist():
    model = report_model()
    model["unresolved"] = ["The required private survey data is unavailable."]
    model["completion"] = {"status": "blocked", "blockers": list(model["unresolved"])}
    draft, parsed = StudyService.format_result(json.dumps(model), report_task())
    assert "## Required user actions\n" not in draft
    assert model["unresolved"][0] not in draft
    assert review_markdown({"report_model": parsed, "completion": parsed["completion"]}).count(model["unresolved"][0]) == 1
    assert parsed["completion"]["status"] == "blocked"
    assert parsed["unresolved"] == model["unresolved"]


def test_required_actions_are_deduplicated_in_checklist_only():
    model = report_model()
    action = "Upload the reviewed PDF to E-learning before the deadline."
    model["completion"] = {"status": "needs_action", "blockers": [],
                           "required_actions": [action, " Verify the required filename. "]}
    model["unresolved"] = [action]
    draft, parsed = StudyService.format_result(json.dumps(model), report_task())
    checklist = review_markdown({"report_model": parsed, "completion": parsed["completion"]})
    assert action not in draft and "Verify the required filename." not in draft
    assert checklist.count(action) == 1
    assert checklist.endswith("- Verify the required filename.\n")
    assert parsed["completion"]["status"] == "needs_action"
    assert "The verified dataset contains 1,200 records." in draft


def test_action_resolutions_preserve_localized_server_action_mapping():
    model = report_model()
    localized = "請於截止日前將審閱後的 PDF 上傳至學校平台。"
    model["completion"] = {"status": "needs_action", "blockers": [],
                           "required_actions": [localized],
                           "action_resolutions": {"action-1": localized}}
    draft, parsed = StudyService.format_result(json.dumps(model), report_task())
    assert localized not in draft
    assert parsed["completion"]["action_resolutions"] == {"action-1": localized}


@pytest.mark.parametrize("resolutions", [None, [], "none", {"": "Upload the report."},
                                        {" ": "Upload the report."}, {"action-1": ""},
                                        {"action-1": " "}, {"action-1": None},
                                        {"action-1": 5}, {"action-1": ["Upload the report."]},
                                        {"action-1": "x" * 4001}])
def test_invalid_action_resolution_mapping_is_rejected(resolutions):
    model = report_model()
    model["completion"]["action_resolutions"] = resolutions
    with pytest.raises(StudyError, match="AI_INVALID_REPORT_SCHEMA"):
        StudyService.format_result(json.dumps(model), report_task())


@pytest.mark.parametrize("heading,body,expected", [
    (None, "本次分析以提供的資料為依據。", "使用者待辦"),
    (None, "This analysis uses the provided evidence.", "Required user actions"),
    ("Actions à effectuer", "This analysis uses the provided evidence.", "Actions à effectuer"),
])
def test_old_action_heading_is_removed_without_stripping_code_or_later_sections(heading, body, expected):
    model = report_model()
    model["sections"][0]["content"] = body
    model["completion"] = {"status": "needs_action", "blockers": [],
                           "required_actions": ["Confirm the submission filename."]}
    if heading:
        model["report_labels"]["actions_heading"] = heading
    draft, parsed = StudyService.format_result(json.dumps(model), report_task())
    old = draft + (f"\n## {expected}\n\n- Confirm the submission filename.\n\n"
                   f"A saved correction.\n\n## Appendix\n\n```text\n## {expected}\n```\n")
    clean = submission_markdown({"draft": old, "report_model": parsed})
    assert "Confirm the submission filename." not in clean
    assert f"```text\n## {expected}\n```" in clean
    assert body in clean and "## Appendix" in clean and "A saved correction." in clean


@pytest.mark.parametrize("mode", ["report_draft", "complete_assignment"])
def test_submission_exports_preserve_assignment_headings_and_saved_edits(service, mode):
    task_id = "reviewed-edit"
    task = {**report_task(mode), "id": task_id, "assignment_id": "2", "status": "waiting_review",
            "request_sha256": "synthetic", "reviewed": False}
    model = report_model()
    model["sources"] = []
    model["sections"].append({"heading": "Submission Checklist", "content": "Recovery tests passed."})
    model["unresolved"] = ["Verify the filename before uploading."]
    if mode == "complete_assignment":
        model["report_labels"]["actions_heading"] = "Submission Checklist"
    draft, parsed = StudyService.format_result(json.dumps(model), task)
    service.store.save(service.user, "task", task_id, {**task, "draft": draft, "report_model": parsed})
    edited = draft.rstrip() + "\n\nA saved data correction.\n"
    service.save_draft(task_id, edited.replace("\n", "\r\n"), reviewed=True)
    exported = export_extra(service, task_id, "zip")
    base = f"/api/artifacts/{task_id}/download?kind="
    with TestClient(create_app(service, "reviewed-edit"), base_url=ORIGIN) as client:
        client.get("/unlock/reviewed-edit", follow_redirects=False)
        markdown = client.get(base + "markdown").text
        archive = zipfile.ZipFile(io.BytesIO(client.get(base + "zip").content))
        with archive, zipfile.ZipFile(io.BytesIO(archive.read("report.docx"))) as document, \
                pymupdf.open(stream=archive.read("report.pdf"), filetype="pdf") as pdf:
            for content in (markdown, archive.read("report.md").decode(),
                            document.read("word/document.xml").decode(), "".join(page.get_text() for page in pdf)):
                assert "Submission Checklist" in content
                assert "Recovery tests passed." in content
                assert "A saved data correction." in content
                assert "待補與人工核對" not in content
                assert model["unresolved"][0] not in content
        assert model["unresolved"][0] in client.get(base + "checklist").text

        # Previously truncated files can retain a valid binary hash. Bind them
        # to the newly computed source text even when it is the whole draft.
        truncated = hashlib.sha256(b"old truncated submission").hexdigest()
        for metadata in (exported["qa"], exported["exports"]["docx"], exported["exports"]["zip"]):
            metadata["submission_sha256"] = truncated
        service.store.save(service.user, "task", task_id, exported)
        assert client.get(f"/api/tasks/{task_id}").json()["data"]["stale_exports"] == ["pdf", "docx", "zip"]
        for kind in ("pdf", "manifest", "docx", "zip"):
            assert client.get(base + kind).json()["error"]["code"] == "REEXPORT_REQUIRED"
        assert client.post(f"/api/tasks/{task_id}/export").json()["data"]["stale_exports"] == ["docx", "zip"]
        result = client.post(f"/api/tasks/{task_id}/export-extra", json={"kind": "zip"}).json()["data"]
        assert result["stale_exports"] == []
        for kind in ("pdf", "manifest", "docx", "zip"):
            assert client.get(base + kind).status_code == 200


@pytest.mark.parametrize("heading", [5, "", "Actions\n## Forged section"])
def test_invalid_action_heading_is_rejected(heading):
    model = report_model()
    model["report_labels"]["actions_heading"] = heading
    with pytest.raises(StudyError, match="AI_INVALID_REPORT_SCHEMA"):
        StudyService.format_result(json.dumps(model), report_task())


def test_source_heading_is_optional_and_omitted_without_sources():
    model = report_model()
    model.pop("report_labels")
    draft, _ = StudyService.format_result(json.dumps(model), report_task())
    assert "## References\n" in draft
    model["sources"] = []
    draft, _ = StudyService.format_result(json.dumps(model), report_task())
    assert "## References\n" not in draft


@pytest.mark.parametrize("labels", [None, [], {"sources_heading": 5},
                                    {"sources_heading": "References\n## Forged section"}])
def test_invalid_localized_heading_is_rejected(labels):
    model = report_model()
    model["report_labels"] = labels
    with pytest.raises(StudyError, match="AI_INVALID_REPORT_SCHEMA"):
        StudyService.format_result(json.dumps(model), report_task())


@pytest.mark.parametrize("completion", [None, [], {"status": "success", "blockers": []},
                                        {"status": "complete", "blockers": "none"},
                                        {"status": "blocked", "blockers": [None]},
                                        {"status": "needs_action", "blockers": [], "required_actions": "none"},
                                        {"status": "needs_action", "blockers": [], "required_actions": [None]}])
def test_invalid_completion_metadata_is_rejected(completion):
    model = report_model()
    model["completion"] = completion
    with pytest.raises(StudyError, match="AI_INVALID_REPORT_SCHEMA"):
        StudyService.format_result(json.dumps(model), report_task())


def test_completed_report_rejects_unprovided_sources():
    model = report_model()
    model["sources"] = ["invented-source"]
    with pytest.raises(StudyError, match="AI_INVALID_REPORT_SCHEMA"):
        StudyService.format_result(json.dumps(model), report_task())


def test_manual_report_retains_existing_review_notes():
    model = report_model()
    draft, _ = StudyService.format_result(json.dumps(model), report_task("report_draft"))
    assert "課程：Data Mining" in draft
    assert "尚未執行程式或實驗" in draft
    assert "## 待補與人工核對" in draft


def test_submission_exports_and_old_drafts_keep_actions_in_a_separate_download(service):
    original = "# Student report\n\n## Analysis\n\nA real result.\n"
    task_id = "submission-export"
    action = "Upload the PDF to the school site."
    task = {**report_task(), "id": task_id, "assignment_id": "2", "status": "waiting_review",
            "draft": original + f"\nRemaining fields: TODO.\n\n## Required user actions\n\n- {action}\n",
            "request_sha256": "synthetic", "reviewed": True,
            "completion": {"status": "needs_action", "required_actions": [action], "blockers": []}}
    service.store.save(service.user, "task", task_id, task)
    exported = export_extra(service, task_id, "zip")
    client = TestClient(create_app(service, "submission-export"), base_url=ORIGIN)
    client.get("/unlock/submission-export", follow_redirects=False)
    base = f"/api/artifacts/{task_id}/download?kind="
    assert client.get(base + "markdown").text.strip() == original.strip()
    assert client.get(base + "pdf").status_code == 200
    checklist = client.get(base + "checklist")
    assert checklist.status_code == 200 and action in checklist.text
    assert "review-checklist.md" in checklist.headers["Content-Disposition"]
    with zipfile.ZipFile(service.safe_path(exported["exports"]["zip"]["path"])) as archive:
        assert archive.read("report.md").decode().strip() == original.strip()
        assert "review-checklist.md" not in archive.namelist()
        assert "unresolved" not in json.loads(archive.read("manifest.json"))
        with zipfile.ZipFile(io.BytesIO(archive.read("report.docx"))) as document:
            assert b"A real result." in document.read("word/document.xml")
            assert action.encode() not in document.read("word/document.xml")
        with pymupdf.open(stream=archive.read("report.pdf"), filetype="pdf") as pdf:
            body = "".join(page.get_text() for page in pdf)
            assert "A real result." in body
            assert "Required user actions" not in body and action not in body
            assert "AI 協作草稿" not in body
    exported["qa"].pop("submission_sha256")
    exported["exports"]["docx"].pop("submission_sha256")
    exported["exports"]["zip"].pop("submission_sha256")
    service.store.save(service.user, "task", task_id, exported)
    for kind in ("pdf", "docx", "zip"):
        assert client.get(base + kind).json()["error"]["code"] == "REEXPORT_REQUIRED"
    exported = export_extra(service, task_id, "zip")
    assert client.get(base + "zip").status_code == 200
    exported["exports"]["zip"].pop("separate_review_checklist")
    service.store.save(service.user, "task", task_id, exported)
    assert client.get(base + "zip").json()["error"]["code"] == "REEXPORT_REQUIRED"


@pytest.mark.parametrize("body", ["No submission content is available yet.", "Findings: TODO.", "```\nTBD\n```"])
def test_status_only_direct_completion_cannot_export_as_a_report(service, body):
    task_id = "only-status"
    task = {**report_task(), "id": task_id, "assignment_id": "2", "status": "waiting_review",
            "draft": f"# HW1\n\n## Status\n\n{body}\n",
            "request_sha256": "synthetic", "reviewed": True, "ready_for_export": True}
    service.store.save(service.user, "task", task_id, task)
    for export in (service.export, lambda ref: export_extra(service, ref, "docx"),
                   lambda ref: export_extra(service, ref, "zip")):
        with pytest.raises(StudyError, match="SUBMISSION_CONTENT_MISSING"):
            export(task_id)
    assert service.get("task", task_id)["status"] == "waiting_review"
    client = TestClient(create_app(service, "only-status"), base_url=ORIGIN)
    client.get("/unlock/only-status", follow_redirects=False)
    response = client.get(f"/api/artifacts/{task_id}/download?kind=markdown")
    assert response.json()["error"]["code"] == "SUBMISSION_CONTENT_MISSING"
