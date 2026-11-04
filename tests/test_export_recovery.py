"""PDF bounds and independent source downloads using synthetic program drafts."""

import concurrent.futures
import hashlib
import io
import json
import zipfile

import pymupdf
import pytest
from fastapi.testclient import TestClient
from test_mvp import await_task, task_request
from test_mvp import service as service

from backend.main import ORIGIN, create_app
from backend.p0.moodle import probe
from backend.services.exports import export_extra
from backend.services.reports import render_pdf, validate_pdf
from backend.services.study import StudyError
from frontend.gradio_app import build_ui


def code_task(service):
    def begin(prompt):
        future = concurrent.futures.Future()
        future.set_result(json.dumps({"title": "合成程式草稿", "sections": [{"heading": "程式",
            "content": "```python\ndef solve():\n    return 2 ** 8\n```"}],
            "sources": ["a" * 24], "unresolved": []}))
        return "synthetic", future
    service.ai.begin = begin
    task = service.create_task({**task_request(service), "mode": "code_solution",
                               "data_transfer_approved": True}, "export-recovery")
    task = await_task(service, task["id"])
    assert task["status"] == "waiting_review"
    return service.save_draft(task["id"], task["draft"], reviewed=True, accept_incomplete=True)


def test_large_cjk_heading_wraps_punctuation_inside_pdf_bounds(tmp_path):
    target = tmp_path / "long-heading.pdf"
    title = "資料" * 11 + "表）"
    result = render_pdf(f"# {title}\n\n## 解題說明\n這是合成長標題測試。", target)
    assert result["passed"] and validate_pdf(target)["passed"]
    with pymupdf.open(target) as pdf:
        assert title in "".join(page.get_text() for page in pdf).replace("\n", "")


def test_pdf_code_preserves_indentation_operators_and_literal_markup(tmp_path):
    target = tmp_path / "program.pdf"
    render_pdf('# 程式測試\n\n```python\ndef solve():\n'
               '    text = "`literal` ** <tag> &"\n    return 2 ** 8\n```', target)
    with pymupdf.open(target) as pdf:
        text = "".join(page.get_text() for page in pdf)
        assert '`literal` ** <tag> &' in text and "return 2 ** 8" in text
        words = [word for page in pdf for word in page.get_text("words")]
        definition = next(word for word in words if word[4] == "def")
        returned = next(word for word in words if word[4] == "return")
        assert returned[0] > definition[0] + 4
    assert validate_pdf(target)["passed"]


@pytest.mark.parametrize("docx_failed", [False, True])
def test_pdf_failure_keeps_code_downloadable_and_zip_exports_available_files(service, monkeypatch, docx_failed):
    task = code_task(service)
    def pdf_failure(*args, **kwargs):
        raise probe.ProbeError("PDF_QA_FAILED")
    monkeypatch.setattr("backend.services.study.render_pdf", pdf_failure)
    if docx_failed:
        def docx_failure(*args, **kwargs):
            raise StudyError("DOCX_TABLE_TOO_WIDE")
        monkeypatch.setattr("backend.services.exports.render_docx", docx_failure)
    callbacks = {e.fn.__name__: e.fn for e in build_ui(service).fns.values() if e.fn}
    message, links = callbacks["export"](task["id"], task["draft"], True)
    assert "PDF_QA_FAILED" in message
    assert "solution_1.py" in links and "kind=markdown" in links
    message, links = callbacks["export_additional"](task["id"], task["draft"], True, "zip", False)
    assert "ZIP 已匯出" in message and "PDF_QA_FAILED" in message and "kind=zip" in links
    current = service.get("task", task["id"])
    with zipfile.ZipFile(service.safe_path(current["exports"]["zip"]["path"])) as archive:
        assert "report.pdf" not in archive.namelist()
        assert ("report.docx" in archive.namelist()) is not docx_failed
        assert "return 2 ** 8" in archive.read("code/solution_1.py").decode()
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["omitted_exports"]["pdf"] == "PDF_QA_FAILED"
        for name, digest in manifest["artifacts"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    with TestClient(create_app(service, "export-test"), base_url=ORIGIN) as client:
        client.get("/unlock/export-test")
        assert client.get(f"/api/tasks/{task['id']}/artifacts/solution_1.py").status_code == 200
        response = client.get(f"/api/artifacts/{task['id']}/download?kind=zip")
        assert response.status_code == 200
        assert zipfile.ZipFile(io.BytesIO(response.content)).testzip() is None


def test_zip_does_not_hide_unexpected_pdf_or_filesystem_errors(service, monkeypatch):
    task = code_task(service)
    def unexpected(*args, **kwargs):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr("backend.services.study.render_pdf", unexpected)
    with pytest.raises(OSError):
        export_extra(service, task["id"], "zip")
    assert not service.get("task", task["id"]).get("exports")


@pytest.mark.parametrize("legacy_windows_snapshot", [False, True])
def test_zip_sources_match_approved_hash_with_windows_newline_compatibility(service, legacy_windows_snapshot):
    assignment = service.get("assignment", "2")
    description = "第一行題目\n第二行要求\n第三行說明\n"
    service.store.save(service.user, "assignment", "2", {**assignment, "description": description})
    task = code_task(service)
    snapshot = service.workspace(service.user, task["id"]) / "input/assignment-description.txt"
    approved = description.encode("utf-8")
    assert snapshot.read_bytes() == approved
    if legacy_windows_snapshot:
        snapshot.write_bytes(approved.replace(b"\n", b"\r\n"))
    original_snapshot = snapshot.read_bytes()
    task = export_extra(service, task["id"], "zip", include_sources=True)
    with zipfile.ZipFile(service.safe_path(task["exports"]["zip"]["path"])) as archive:
        assert archive.read("sources/assignment-description.txt") == approved
        manifest = json.loads(archive.read("manifest.json"))
        assert hashlib.sha256(approved).hexdigest() == manifest["source_hashes"]["assignment-description"]
        for name, digest in manifest["artifacts"].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == digest
    # Compatibility is local to ZIP assembly, leaving historical files intact.
    assert snapshot.read_bytes() == original_snapshot


@pytest.mark.parametrize("changed_file", ["description", "attachment", "docx"])
def test_zip_rejects_real_content_changes_and_preserves_existing_exports(service, changed_file):
    task = code_task(service)
    task = export_extra(service, task["id"], "zip")
    word_path = service.safe_path(task["exports"]["docx"]["path"])
    zip_path = service.safe_path(task["exports"]["zip"]["path"])
    previous_word, previous_zip = word_path.read_bytes(), zip_path.read_bytes()
    workspace = service.workspace(service.user, task["id"])
    if changed_file == "description":
        (workspace / "input/assignment-description.txt").write_bytes(b"different content\r\n")
    elif changed_file == "attachment":
        digest = task["source_hashes"]["a" * 24]
        (workspace / "input" / (digest + ".pdf")).write_bytes(b"different attachment")
    else:
        word_path.write_bytes(b"different Word document")
    with pytest.raises(StudyError, match="ARTIFACT_CHANGED"):
        export_extra(service, task["id"], "zip", include_sources=True)
    assert zip_path.read_bytes() == previous_zip
    if changed_file != "docx":
        assert word_path.read_bytes() == previous_word
    assert service.get("task", task["id"])["exports"] == task["exports"]
