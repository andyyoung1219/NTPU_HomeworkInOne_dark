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


def test_legacy_blockers_and_unresolved_are_visible_in_report_actions():
    model = report_model()
    model["unresolved"] = ["The required private survey data is unavailable."]
    model["completion"] = {"status": "blocked", "blockers": list(model["unresolved"])}
    draft, parsed = StudyService.format_result(json.dumps(model), report_task())
    assert "## Required user actions\n" in draft
    assert draft.count(model["unresolved"][0]) == 1
    assert parsed["completion"]["status"] == "blocked"
    assert parsed["unresolved"] == model["unresolved"]


def test_required_actions_are_deduplicated_and_appended_after_report_sources():
    model = report_model()
    action = "Upload the reviewed PDF to E-learning before the deadline."
    model["completion"] = {"status": "needs_action", "blockers": [],
                           "required_actions": [action, " Verify the required filename. "]}
    model["unresolved"] = [action]
    draft, parsed = StudyService.format_result(json.dumps(model), report_task())
    assert draft.count(action) == 1
    assert draft.index("## References") < draft.index("## Required user actions")
    assert draft.endswith("- Verify the required filename.\n")
    assert parsed["completion"]["status"] == "needs_action"
    assert "The verified dataset contains 1,200 records." in draft


def test_action_resolutions_preserve_localized_server_action_mapping():
    model = report_model()
    localized = "請於截止日前將審閱後的 PDF 上傳至學校平台。"
    model["completion"] = {"status": "needs_action", "blockers": [],
                           "required_actions": [localized],
                           "action_resolutions": {"action-1": localized}}
    draft, parsed = StudyService.format_result(json.dumps(model), report_task())
    assert localized in draft
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
def test_action_heading_follows_report_language_or_explicit_label(heading, body, expected):
    model = report_model()
    model["sections"][0]["content"] = body
    model["completion"] = {"status": "needs_action", "blockers": [],
                           "required_actions": ["Confirm the submission filename."]}
    if heading:
        model["report_labels"]["actions_heading"] = heading
    draft, _ = StudyService.format_result(json.dumps(model), report_task())
    assert f"## {expected}\n" in draft


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
