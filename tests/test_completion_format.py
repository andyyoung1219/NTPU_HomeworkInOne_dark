"""The direct-completion deliverable is separated from app review metadata."""

import json

import pytest

from backend.services.study import StudyError, StudyService


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


def test_blockers_stay_available_to_review_ui_without_entering_report_body():
    model = report_model()
    model["unresolved"] = ["The required private survey data is unavailable."]
    model["completion"] = {"status": "blocked", "blockers": list(model["unresolved"])}
    draft, parsed = StudyService.format_result(json.dumps(model), report_task())
    assert model["unresolved"][0] not in draft
    assert parsed["completion"]["status"] == "blocked"
    assert parsed["unresolved"] == model["unresolved"]


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
                                        {"status": "blocked", "blockers": [None]}])
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
