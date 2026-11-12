"""Direct completion selects bounded source evidence for each stage."""

import json

from backend.agent.completion_context import planning_context, unit_context


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
