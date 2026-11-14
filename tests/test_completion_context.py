"""Direct completion selects bounded source evidence for each stage."""

import json

import pytest

from backend.agent.completion import model_object, recovered_markdown
from backend.agent.completion_context import grounded_actions, planning_context, unit_context
from backend.services.documents import requirements
from backend.services.study import StudyError


def test_planner_and_unit_can_use_evidence_beyond_the_global_excerpt():
    assignment = {"file_id": "assignment-description", "section": 1, "text": "Submit both results."}
    earlier = [{"file_id": "notes", "page": n, "text": f"Notes page {n}\n" + "a" * 10000}
               for n in range(1, 90)]
    late = {"file_id": "rubric", "page": 7, "text": "Compare the verified output with the baseline."}
    snapshot = {"excerpt": "old global preview", "source_chunks": [assignment, *earlier, late],
                "assets": [{"id": "notes"}, {"id": "rubric"}], "requirements": [
                    {"id": "REQ-01", "source": {"file_id": "rubric", "page": 7}, "text": late["text"]},
                ]}
    plan = planning_context(snapshot)
    assert "Notes page 89" in plan["excerpt"] and late["text"] in plan["excerpt"]
    assert plan["source_index_limited"]
    assert len(json.dumps(plan["source_index"], ensure_ascii=False)) <= 8000
    assert any(entry["file_id"] == "rubric" and entry["page"] == 7 for entry in plan["source_index"])

    context = unit_context(snapshot, {"source_refs": [{"file_id": "rubric", "page": 7}]})
    assert late["text"] in context["excerpt"] and assignment["text"] in context["excerpt"]
    assert "Notes page" not in context["excerpt"]
    assert context["assets"] == [{"id": "rubric"}]
    assert context["requirements"][0]["id"] == "REQ-01"
    assert not context["missing_locations"] and not context["context_limited"]


def test_precise_source_ref_requires_the_requested_page_or_section():
    snapshot = {"source_chunks": [
        {"file_id": "attachment", "section": 1, "text": "The DOCX section."},
        {"file_id": "attachment", "page": 1, "text": "The requested PDF page."},
        {"file_id": "attachment", "page": 2, "text": "The unrelated PDF page."},
    ]}
    missing = {"file_id": "attachment", "page": 99}
    context = unit_context(snapshot, {"source_refs": [{"file_id": "attachment", "page": 1}, missing]})
    assert "The requested PDF page" in context["excerpt"]
    assert "DOCX section" not in context["excerpt"] and "unrelated PDF page" not in context["excerpt"]
    assert context["missing_locations"] == [missing]


def test_legacy_excerpt_prioritizes_location_hints_and_compact_mode_is_bounded():
    snapshot = {"excerpt": "[來源 assignment-description / 頁或節 1]\nFollow these instructions.\n\n"
                "[來源 attachment / 頁或節 1]\n" + "A" * 12000 + "\n\n"
                "[來源 attachment / 頁或節 8]\nUse this evidence for the analysis."}
    context = unit_context(snapshot, {"source_ids": ["attachment"], "source_locations": ["第 8 頁"]}, compact=True)
    assert context["excerpt"].startswith("[來源 attachment / 頁或節 8]\nUse this evidence")
    assert "Follow these instructions." in context["excerpt"]
    assert context["context_limited"] and len(context["excerpt"]) <= 4000
    assert unit_context({"excerpt": "plain legacy evidence"}, {})["excerpt"] == "plain legacy evidence"


def test_control_heavy_excerpt_keeps_assignment_instructions_inside_the_wire_budget():
    assignment = {"file_id": "assignment-description", "section": 1, "text": "Follow the rubric."}
    pages = [{"file_id": "attachment", "page": n, "text": f"Page {n}\n" + "\"🧮\\\t" * 10000}
             for n in range(1, 30)]
    pages[0]["truncated"] = True
    blank = {"file_id": "attachment", "page": 100, "text": "  "}
    snapshot = {"source_chunks": [assignment, *pages, blank]}
    context = unit_context(snapshot, {"source_ids": ["attachment"]}, compact=True)
    assert assignment["text"] in context["excerpt"]
    assert len(json.dumps(context["excerpt"], ensure_ascii=False).encode("utf-16-le")) // 2 <= 4002
    assert context["context_limited"]
    index = planning_context(snapshot)["source_index"]
    assert any(item.get("page") == 1 and item.get("truncated") for item in index)
    assert all(item.get("page") != 100 for item in index)


def test_planning_uses_full_excerpt_budget_for_moodle_only_assignment():
    text = "Background to the assignment. " * 120 + "Calculate the final result as well."
    context = planning_context({"source_chunks": [{"file_id": "assignment-description", "section": 1, "text": text}]})
    assert "Calculate the final result as well." in context["excerpt"]
    assert not context["assignment_context_limited"]


def test_actions_use_one_verbatim_mandatory_excerpt_per_requirement():
    requirements = [
        {"id": "REQ-01", "text": "Submit model.json with the coefficients. Optional: compare holdout RMSE."},
        {"id": "REQ-02", "text": "2. 選做：請比較訓練資料的 MAE。"},
    ]
    proposals = [
        {"requirement_id": "REQ-01", "quote": "compare holdout RMSE"},
        {"requirement_id": "REQ-02", "quote": "請比較訓練資料的 MAE"},
        {"requirement_id": "REQ-01", "quote": "Submit model.json with the coefficients"},
        {"requirement_id": "REQ-01", "quote": "with the coefficients"},
        {"requirement_id": "REQ-03", "quote": "Submit the interactive controls"},
    ]
    assert grounded_actions(requirements, proposals) == {"REQ-01": "Submit model.json with the coefficients"}


def test_rubric_labels_and_grading_rows_keep_mandatory_and_optional_requirements():
    chunks = [{"file_id": "assignment-description", "section": 1, "text": (
        "Rubric:\n- Reproduction bundle\nInteractive slider | 20 points\n"
        "選做項目：\n- 額外的互動圖表\n背景：\n2026 年度課程\n模型對照（占總成績 20％）\n資料切分比例（20％）"
    )}]
    items = requirements(chunks)
    assert [(item["text"], item.get("optional", False)) for item in items] == [
        ("- Reproduction bundle", False), ("Interactive slider | 20 points", False),
        ("- 額外的互動圖表", True), ("模型對照（占總成績 20％）", False),
    ]


def test_proposal_requirements_include_numbered_directives_and_cloud_link():
    chunks = [{"file_id": "assignment-description", "section": 1, "text": (
        "Make a proposal for your data mining project.\n"
        "1. Use a data set with at least 1,000 data records.\n"
        "2. Introduce the dataset, including its source, attributes and statistics.\n"
        "NOTE: Just choose five examples. Please include a cloud link to the complete dataset.\n"
        "3. Define your mining task.\n"
        "4. Submit the above content in English as a PDF.\n"
        "PS: The final project is due December 11. Please plan your time."
    )}]
    items = [item["text"] for item in requirements(chunks)]
    assert len(items) == 6
    assert items[1].startswith("1. Use") and items[2].startswith("2. Introduce")
    assert items[3].startswith("Please include a cloud link") and items[4].startswith("3. Define")
    assert not any("December" in item for item in items)


def test_note_extracts_first_and_later_directives_with_individual_optionality():
    items = requirements([{"file_id": "assignment-description", "section": 3, "text": (
        "Note: Submit your final PDF to Moodle. The report template is on Moodle. "
        "Include a cloud link. Optional: upload a short recording.\n"
        "備註：請上傳原始資料。參考資料已公告。須附上分析程式。選做：提供結果圖。\n"
        "Note: The example must be opened using the old viewer.\n"
        "備註：請注意上傳網站將在週末維護。\n"
        "PS: Please plan your time."
    )}])

    assert [(item["text"], item.get("optional", False)) for item in items] == [
        ("Submit your final PDF to Moodle.", False),
        ("Include a cloud link.", False),
        ("Optional: upload a short recording.", True),
        ("請上傳原始資料", False),
        ("須附上分析程式", False),
        ("選做：提供結果圖", True),
    ]
    for index, item in enumerate(items, start=1):
        assert item["id"] == f"REQ-{index:02}"
        assert item["source"] == {"file_id": "assignment-description", "section": 3}


@pytest.mark.parametrize("answer", [
    'Here is the response:\n```json\n{"content":"A result."}\n```\nDone.',
    'Here is the response:\n{\n  "content": "A result."\n}\nDone.',
    '\ufeff{"content":"A result."}',
])
def test_single_wrapped_model_object_is_preserved(answer):
    assert model_object(answer) == {"content": "A result."}


@pytest.mark.parametrize("answer", [
    '```json\n{"content":"First."}\n```\n```json\n{"content":"Second."}\n```',
    '[\n{"content":"An object inside an array."}\n]',
])
def test_model_response_must_have_one_top_level_object(answer):
    with pytest.raises(StudyError, match="AI_INVALID_REPORT_SCHEMA"):
        model_object(answer)


@pytest.mark.parametrize("raw", [
    "### Answer\n\nTODO: complete the work", "# Heading only", "```text\nunfinished", "{bad JSON",
    "The response ended before providing a JSON object.",
])
def test_recovery_rejects_empty_or_malformed_markdown(raw):
    assert recovered_markdown(raw, []) is None
