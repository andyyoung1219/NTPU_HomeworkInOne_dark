"""End-to-end direct completion with bounded deliverables and execution evidence."""

import concurrent.futures
import hashlib
import io
import json
import os
import zipfile

import pymupdf
import pytest
from PIL import Image
from test_completion import automatic as automatic
from test_mvp import await_task
from test_mvp import service as service

from backend.agent import completion
from backend.agent.contracts import digest
from backend.agent.runner import DockerRunner, filesystem_path
from backend.p0.ai import AIError
from backend.p0.evidence import ROOT
from backend.services.exports import export_extra
from backend.services.study import StudyError

MEASUREMENT = b'{"sum":6}'
MEASUREMENT_SHA256 = hashlib.sha256(MEASUREMENT).hexdigest()
MEASUREMENT_INPUT = MEASUREMENT_SHA256 + ".json"
MEASUREMENT_CODE = """import json
from pathlib import Path
Path("/output/measurement.json").write_text(json.dumps({"sum": sum([1, 2, 3])}, separators=(",", ":")))
"""
VERIFICATION = b'{"sum":6,"verified":true}'
VERIFICATION_CODE = f"""import json
from pathlib import Path
measurement = json.loads(Path("/input/{MEASUREMENT_INPUT}").read_text())
assert measurement["sum"] == 6
Path("/output/verification.json").write_text(json.dumps({{"sum": measurement["sum"], "verified": True}}, separators=(",", ":")))
"""


def deliverable(identifier, title, kind, **options):
    return {
        "id": identifier,
        "title": title,
        "objective": title,
        "kind": kind,
        "dependencies": [],
        "source_ids": [],
        "required_capabilities": [],
        "expected_artifacts": [],
        **options,
    }


class StructuredAI:
    def __init__(self, deliverables, responses, blockers=None, **plan_options):
        self.deliverables = deliverables
        self.responses = responses
        self.blockers = blockers or []
        self.plan_options = plan_options
        self.calls = []

    def rpc(self, method, **kwargs):
        return {"ready": True}

    def begin(self, prompt):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        self.calls.append(payload)
        if "dataset_catalog" in payload:
            deliverables = self.deliverables
            if not any("requirement_ids" in unit for unit in deliverables):
                ids = [item["id"] for item in payload["inputs"]["requirements"]]
                deliverables = [{**unit, "requirement_ids": ids if index == 0 else []}
                                for index, unit in enumerate(deliverables)]
            result = {"objective": "Complete the approved tasks.",
                      "deliverable_type": "report", "report_language": "en",
                      "blockers": self.blockers,
                      "deliverables": deliverables, **self.plan_options}
        else:
            result = self.responses[payload["deliverable"]["id"]]
        future = concurrent.futures.Future()
        if isinstance(result, Exception):
            future.set_exception(result)
        else:
            future.set_result(result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))
        return "synthetic-model", future

    def close(self):
        pass


def python_plan():
    # Return the dependency second to test server-owned topological ordering.
    return [
        deliverable("verify", "Verify measurements", "python", dependencies=["measure"],
                    required_capabilities=["python-standard-library"],
                    expected_artifacts=["verification.json"]),
        deliverable("measure", "Measure sample values", "python",
                    required_capabilities=["python-standard-library"],
                    expected_artifacts=["measurement.json"]),
    ]


def code_responses():
    return {"measure": {"code": MEASUREMENT_CODE}, "verify": {"code": VERIFICATION_CODE}}


def test_long_structured_writer_response_keeps_prose_without_requiring_metadata(automatic):
    assignment = automatic.get("assignment", "2")
    assignment.update(asset_ids=[], description="Write the proposal in English.")
    automatic.store.save(automatic.user, "assignment", "2", assignment)
    prose = ("This paragraph presents the selected research question and method. " * 120).strip()
    response = {"sections": [{"heading": "Method", "content": prose}], "sources": ["unknown-source"]}
    automatic.ai = StructuredAI([
        deliverable("proposal", "Project proposal", "writing", source_ids=["assignment-description"],
                    requirement_ids=["REQ-01", "REQ-99"]),
    ], {"proposal": "Draft follows\n```json\n" + json.dumps(response) + "\n```"})
    result = await_task(automatic, automatic.complete_assignment("2", "long-structured-report")["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "complete"
    assert result["coverage"][0]["requirement_ids"] == ["REQ-01"]
    assert result["report_model"]["sources"] == []
    assert result["checkpoint"]["attempts"]["unit-proposal"] == 1
    assert "### Method" in result["draft"] and prose in result["draft"]


def test_staged_markdown_retry_keeps_answer_sources_and_mandatory_actions(automatic):
    assignment = automatic.get("assignment", "2")
    assignment["description"] = "Explain the calculation.\nInclude the original answer sheet."
    automatic.store.save(automatic.user, "assignment", "2", assignment)
    source = assignment["asset_ids"][0]
    aid = automatic.ai = StructuredAI([
        deliverable("solution", "Worked calculation", "writing", source_ids=[source],
                    requirement_ids=["REQ-01", "REQ-02", "REQ-03"]),
    ], {"solution": "not structured JSON"})
    original = aid.begin
    calls = []

    def begin(prompt):
        calls.append(prompt)
        if "格式修復" in prompt:
            aid.responses["solution"] = (
                "### Calculation\n\nThe sum is six because the three inputs add to 1 + 2 + 3.\n\n"
                f"[[source:{source}]]\n[[source:unknown-source]]\n"
                "[[pending:REQ-02]] Include the original answer sheet."
            )
        return original(prompt)

    aid.begin = begin
    task = await_task(automatic, automatic.complete_assignment("2", "markdown-retry")["id"], timeout=15)
    assert task["status"] == "waiting_review", task.get("error")
    assert task["checkpoint"]["attempts"]["unit-solution"] == 2
    assert "格式修復" not in calls[-2] and "格式修復" in calls[-1]
    assert "The sum is six" in task["draft"] and "[[source:" not in task["draft"]
    assert task["report_model"]["sources"] == [source]
    assert "Include the original answer sheet" in task["completion"]["required_actions"][0]
    assert "recovered from Markdown" in task["completion"]["review_required"][0]
    assert task["completion"]["status"] == "needs_action"


def test_malformed_proposal_writer_recovers_actual_snapshot_statistics_and_examples(automatic):
    service = automatic
    source = "d" * 24
    data = b"group,value\nA,1\nA,4\nB,7\nB,9\nB,10\n"
    (service.root / "study-data.csv").write_bytes(data)
    assignment = service.get("assignment", "2")
    assignment.update(asset_ids=[source], description="Make a proposal for your data mining project.")
    service.store.save(service.user, "assignment", "2", assignment)
    service.store.save(service.user, "asset", source, {
        "id": source, "assignment_id": "2", "name": "study-data.csv", "mime_type": "text/csv",
        "path": "study-data.csv", "status": "downloaded", "sha256": hashlib.sha256(data).hexdigest(),
    })
    service.ai = StructuredAI([
        deliverable("proposal", "Data mining proposal", "writing", source_ids=["assignment-description"],
                    requirement_ids=["REQ-01"]),
    ], {"proposal": "The response ended before providing a JSON object."}, deliverable_type="proposal")
    result = await_task(service, service.complete_assignment("2", "recovered-proposal")["id"], timeout=15)
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "needs_action"
    assert result["tool_results"]["unit-proposal"]["error_code"] == "AI_INVALID_REPORT_SCHEMA"
    assert "5 records and 2 columns" in result["draft"]
    assert "| value | numeric | 0 | 1 | 6.2 | 10 |" in result["draft"]
    assert "| 5 | B | 10 |" in result["draft"]
    assert "recovered from" not in result["draft"]
    assert service.ai.calls[-1]["data"]["attachment_profiles"][0]["profile"]["examples"][-1]["value"] == "10"


def test_report_lists_pca_once_and_keeps_optional_metrics_in_prose(automatic):
    assignment = automatic.get("assignment", "2")
    assignment.update(asset_ids=[], description=(
        "必須執行 NumPy PCA 並繳交可操作的主成分滑桿。\n"
        "報告須比較各組 MAE 並說明研究限制。\n"
        "選做：請比較保留資料的基準 RMSE。"
    ))
    automatic.store.save(automatic.user, "assignment", "2", assignment)
    automatic.ai = StructuredAI([
        deliverable("pca", "NumPy PCA slider", "manual", requirement_ids=["REQ-01"],
                    required_capabilities=["numpy", "interactive-visualization"]),
        deliverable("rmse", "Compare baseline RMSE", "manual", requirement_ids=["REQ-03"]),
        deliverable("extra-metric", "Calculate training MAE", "manual", optional=True),
        deliverable("interactive-view", "Describe how to operate the slider", "writing",
                    requirement_ids=["REQ-01"], dependencies=["pca"]),
        deliverable("interpret", "Explain group errors", "writing", requirement_ids=["REQ-02"],
                    dependencies=["rmse"], optional_dependencies=["pca"]),
    ], {"interpret": {
        "content": "The MAE comparison uses each group's available results. Baseline RMSE is an optional extension. An optional chart is TBD.",
        "sources": [],
        "required_actions": [
            {"requirement_id": "REQ-01", "quote": "必須執行 NumPy PCA 並繳交可操作的主成分滑桿"},
            {"requirement_id": "REQ-02", "quote": "保留資料的基準 RMSE 與訓練資料的 MAE"},
            {"requirement_id": "REQ-03", "quote": "請比較保留資料的基準 RMSE"},
            "Compute training MAE for an additional comparison.",
        ],
    }}, blockers=["NumPy is unavailable (REQ-01).", "Interactive controls are unavailable (REQ-01)."])

    task = automatic.complete_assignment("2", "structured-required-only")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    actions = result["completion"]["required_actions"]
    assert len(actions) == 1
    assert "NumPy PCA slider (REQ-01)" in actions[0] and "RMSE" not in actions[0]
    assert result["tool_results"]["unit-interactive-view"]["blocked_by"] == ["pca"]
    assert next(item for item in result["coverage"] if item["deliverable_id"] == "extra-metric")["optional"]
    assert result["tool_results"]["unit-interpret"]["status"] == "ready_for_review"
    assert "Baseline RMSE is an optional extension." in result["draft"]
    assert "## Required user actions" not in result["draft"]


@pytest.mark.parametrize("rubric_line, requirement_id", [
    ("2. Interactive slider (70% of grade)", "REQ-02"),
    ("Interactive slider", None),
])
def test_incomplete_unmapped_stage_never_counts_as_optional(automatic, rubric_line, requirement_id):
    assignment = automatic.get("assignment", "2")
    assignment.update(asset_ids=[], description="1. Submit an interpretation (30% of grade).\n" + rubric_line)
    automatic.store.save(automatic.user, "assignment", "2", assignment)
    automatic.ai = StructuredAI([
        deliverable("report", "Interpretation", "writing", requirement_ids=["REQ-01"]),
        deliverable("slider", "Interactive slider", "manual", optional=False,
                    requirement_ids=[requirement_id] if requirement_id else [],
                    required_capabilities=["interactive-visualization"]),
    ], {"report": {"content": "Interpretation draft.", "sources": [], "required_actions": []}})
    result = await_task(automatic, automatic.complete_assignment("2", "missing-slider")["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "needs_action"
    slider = next(item for item in result["coverage"] if item["deliverable_id"] == "slider")
    assert not slider.get("optional")
    if requirement_id:
        assert slider["action_requirement_ids"] == [requirement_id]
        assert "REQ-02" in result["completion"]["required_actions"][0]
    else:
        assert slider["status"] == "needs_review"
        assert slider["requirement_status"] == "unconfirmed"
        assert result["completion"]["required_actions"] == []
        assert result["completion"]["review_required"] == [slider["review_notice"]]
        assert "Check the assignment to confirm whether it is required" in slider["review_notice"]
        assert "Check the assignment" not in result["draft"]
    assert not automatic.save_draft(result["id"], result["draft"], reviewed=True)["ready_for_export"]


def test_unmapped_writing_stage_preserves_explicit_missing_work_for_review(automatic):
    assignment = automatic.get("assignment", "2")
    assignment.update(asset_ids=[], description="Submit a data summary.\nBackground figures")
    automatic.store.save(automatic.user, "assignment", "2", assignment)
    automatic.ai = StructuredAI([
        deliverable("summary", "Data summary", "writing", requirement_ids=["REQ-01"]),
        deliverable("figures", "Background figures", "writing"),
    ], {
        "summary": {"content": "Available observations are summarized.", "sources": [], "required_actions": []},
        "figures": {"content": "No figures were available.", "sources": [], "required_actions": [
            {"requirement_id": "REQ-02", "quote": "Background figures"},
        ]},
    })
    task = await_task(automatic, automatic.complete_assignment("2", "unmapped-writing")["id"])
    assert task["coverage"][-1]["status"] == "needs_review"
    assert task["completion"]["status"] == "needs_action"
    assert task["completion"]["required_actions"] == []


def test_unavailable_capabilities_and_sources_leave_independent_writing_available(automatic, monkeypatch):
    service = automatic
    assignment = service.get("assignment", "2")
    assignment.update(asset_ids=[], description=(
        "Submit the required numerical experiment.\n"
        "Review the attachment required for this task.\n"
        "Provide verified literature citations.\n"
        "Explain the supplied instructions."
    ))
    service.store.save(service.user, "assignment", "2", assignment)
    service.ai = StructuredAI([
        deliverable("numerics", "Required numerical experiment", "python",
                    requirement_ids=["REQ-01"],
                    required_capabilities=["numpy", "interactive-visualization", "embedded-pdf-attachments"]),
        deliverable("attachment", "Review the missing attachment", "writing",
                    source_ids=["c" * 24], requirement_ids=["REQ-02"]),
        deliverable("literature", "Verify external citations", "research", requirement_ids=["REQ-03"]),
        deliverable("outline", "Summarize supplied instructions", "writing",
                    source_ids=["assignment-description"], requirement_ids=["REQ-04"]),
    ], {"outline": {"content": "The assignment asks for an evidence-based report.",
                    "sources": ["assignment-description"], "required_actions": []}})
    monkeypatch.setattr(service.agent.runner, "status", lambda: {"available": True})

    def unexpected(*args, **kwargs):
        pytest.fail("A blocked deliverable must not start an external fetch or a program.")

    monkeypatch.setattr(service.agent.runner, "run", unexpected)
    monkeypatch.setattr(completion.httpx, "Client", unexpected)
    task = service.complete_assignment("2", "structured-capability-gate")
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "needs_action"
    assert [call["deliverable"]["id"] for call in service.ai.calls[1:]] == ["outline"]
    assert [result["tool_results"][f"unit-{unit}"]["status"] for unit in
            ("numerics", "attachment", "literature", "outline")] == [
        "needs_action", "needs_action", "needs_action", "ready_for_review",
    ]
    actions = "\n".join(result["completion"]["required_actions"])
    assert all(term in actions.lower() for term in ("numpy", "attachment", "literature", "interactive", "pdf editor"))
    assert "The assignment asks for an evidence-based report." in result["draft"]
    assert not result.get("runs") and not result.get("reviewed")
    reasons = result["coverage"][0]["reasons"]
    assert any("does not provide numpy" in reason for reason in reasons)
    service.save_draft(result["id"], result["draft"], reviewed=True, accept_incomplete=True)
    exported = export_extra(service, result["id"], "zip")
    package = filesystem_path(service.safe_path(exported["exports"]["zip"]["path"]))
    with zipfile.ZipFile(package) as archive:
        assert b"does not provide numpy" not in archive.read("report.md")
        manifest = json.loads(archive.read("manifest.json"))
        assert "required_actions" not in manifest and "unresolved" not in manifest
        assert "coverage" not in manifest and "review-checklist.md" not in archive.namelist()
        with zipfile.ZipFile(io.BytesIO(archive.read("report.docx"))) as docx:
            assert b"does not provide numpy" not in docx.read("word/document.xml")
        with pymupdf.open(stream=archive.read("report.pdf"), filetype="pdf") as pdf:
            assert "does not provide numpy" not in " ".join(" ".join(page.get_text().split()) for page in pdf)


def test_six_stages_use_late_source_pages_and_continue_with_missing_optional_results(automatic):
    service = automatic
    ref = "a" * 24
    marker = "Late appendix evidence appears here."
    with pymupdf.open() as pdf:
        for index in range(1, 8):
            page = pdf.new_page()
            for line in range(53):
                page.insert_text((45, 50 + line * 13), f"Page {index} background " + "abc " * 19, fontsize=8)
        pdf.new_page().insert_text((55, 70), f"Please submit the appendix result. {marker}")
        data = pdf.tobytes()
    asset = service.get("asset", ref)
    service.safe_path(asset["path"]).write_bytes(data)
    asset["sha256"] = hashlib.sha256(data).hexdigest()
    service.store.save(service.user, "asset", ref, asset)
    plan = [
        deliverable("appendix", "Summarize the appendix", "writing",
                    source_refs=[{"file_id": ref, "page": 8}], requirement_ids=["REQ-02"]),
        deliverable("experiment", "Run the NumPy experiment", "python", required_capabilities=["numpy"]),
        deliverable("comparison", "Compare experiment outputs", "writing", dependencies=["experiment"]),
        deliverable("method", "Describe the available method", "writing", optional_dependencies=["experiment"]),
        deliverable("outline", "Outline the available evidence", "writing", dependencies=["appendix"]),
        deliverable("limitations", "Explain the limitations", "writing", optional_dependencies=["comparison"]),
    ]
    service.ai = StructuredAI(plan, {
        identifier: {"content": f"Draft for {identifier}.", "sources": [ref] if identifier == "appendix" else [],
                     "required_actions": []}
        for identifier in ("appendix", "method", "outline", "limitations")
    })
    task = service.complete_assignment("2", "structured-long-source")
    result = await_task(service, task["id"], timeout=20)
    assert result["status"] == "waiting_review", result.get("error")
    assert marker not in result["completion_inputs"]["excerpt"]
    assert any(item.get("page") == 8 for item in service.ai.calls[0]["inputs"]["source_index"])
    calls = {call["deliverable"]["id"]: call for call in service.ai.calls[1:]}
    assert marker in calls["appendix"]["inputs"]["excerpt"]
    assert "Page 1 background" not in calls["appendix"]["inputs"]["excerpt"]
    assert set(calls) == {"appendix", "method", "outline", "limitations"}
    assert calls["method"]["dependencies"] == [{"deliverable": "experiment", "status": "needs_action"}]
    assert calls["outline"]["dependencies"][0]["draft_context"] == "Draft for appendix."
    assert len(result["coverage"]) == 6
    assert "REQ-01" in "\n".join(result["completion"]["required_actions"])
    assert "Run the NumPy experiment" in "\n".join(result["blockers"])
    assert "Draft for limitations." in result["draft"]


def test_oversize_unit_retries_with_compact_context_before_sending(automatic, monkeypatch):
    service = automatic
    service.ai = StructuredAI([deliverable("summary", "Summarize the text", "writing")], {
        "summary": {"content": "The smaller request completed.", "sources": [], "required_actions": []},
    })
    original_ask = completion.ask

    def ask(engine, user, task, instruction, payload):
        if "deliverable" in payload and not payload["compact_context"]:
            payload = {**payload, "test_large_context": "\\" * 25000}
        return original_ask(engine, user, task, instruction, payload)

    monkeypatch.setattr(completion, "ask", ask)
    task = service.complete_assignment("2", "structured-compact-retry")
    result = await_task(service, task["id"], timeout=12)
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "complete"
    assert result["checkpoint"]["attempts"]["unit-summary"] == 2
    assert result["checkpoint"]["requests_used"] == 2
    assert service.ai.calls[-1]["compact_context"] is True
    assert "The smaller request completed." in result["draft"]


def test_invalid_csv_blocks_its_own_analysis_and_keeps_other_profiles(automatic):
    service = automatic
    valid, invalid = "d" * 24, "e" * 24
    assignment = service.get("assignment", "2")
    assignment["asset_ids"] = [invalid, valid]
    service.store.save(service.user, "assignment", "2", assignment)
    for ref, name, data in [(invalid, "missing-rows.csv", b"x,y\n"), (valid, "readings.csv", b"x,y\n1,4\n2,6\n")]:
        (service.root / name).write_bytes(data)
        service.store.save(service.user, "asset", ref, {
            "id": ref, "assignment_id": "2", "name": name, "mime_type": "text/csv", "path": name,
            "status": "downloaded", "sha256": hashlib.sha256(data).hexdigest(),
        })
    service.ai = StructuredAI([
        deliverable("missing", "Summarize the incomplete file", "writing", source_ids=[invalid],
                    required_capabilities=["csv-profiling"]),
        deliverable("readings", "Summarize the readings", "writing", source_ids=[valid],
                    required_capabilities=["csv-profiling"]),
    ], {"readings": {"content": "Two records were available.", "sources": [valid], "required_actions": []}})
    task = service.complete_assignment("2", "structured-independent-csv")
    result = await_task(service, task["id"], timeout=15)
    assert result["status"] == "waiting_review", result.get("error")
    assert result["tool_results"]["unit-missing"]["status"] == "needs_action"
    assert result["tool_results"]["unit-readings"]["status"] == "ready_for_review"
    profile = service.ai.calls[-1]["data"]["attachment_profiles"][0]
    assert profile["file_id"] == valid and profile["profile"]["records"] == 2
    assert "missing-rows.csv" in "\n".join(result["tool_results"]["unit-missing"]["reasons"])
    assert "Two records were available." in result["draft"]


def test_invalid_dependency_graph_never_becomes_an_executable_plan(automatic):
    service = automatic
    service.ai = StructuredAI([
        deliverable("first", "First item", "writing", dependencies=["second"]),
        deliverable("second", "Second item", "writing", dependencies=["first"]),
    ], {})
    task = service.complete_assignment("2", "structured-cyclic-plan")
    result = await_task(service, task["id"], timeout=10)
    assert result["status"] == "partial"
    assert result["error"] == "AI_INVALID_COMPLETION_PLAN"
    assert all("dataset_catalog" in call for call in service.ai.calls)
    assert all(step["tool"] != "complete_deliverable" for step in result["plan"]["steps"])


def test_missing_runner_keeps_a_reviewable_reminder_when_every_unit_is_blocked(automatic, monkeypatch):
    service = automatic
    service.ai = StructuredAI(python_plan(), code_responses())
    monkeypatch.setattr(service.agent.runner, "status", lambda: {"available": False})
    monkeypatch.setattr(service.agent.runner, "run", lambda *args: pytest.fail("Docker was unavailable"))
    task = service.complete_assignment("2", "structured-runner-missing")
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "needs_action"
    assert [call["deliverable"]["id"] for call in service.ai.calls[1:]] == ["measure"]
    assert not result.get("runs")
    program = result["generated_files"]["deliverable-measure.py"]
    assert filesystem_path(service.safe_path(program["path"])).read_text(encoding="utf-8") == MEASUREMENT_CODE
    assert "Docker Runner" in "\n".join(result["tool_results"]["unit-measure"]["reasons"])
    assert result["tool_results"]["unit-verify"]["status"] == "needs_action"
    reviewed = service.save_draft(result["id"], result["draft"], reviewed=True)
    assert not reviewed["ready_for_export"]
    with pytest.raises(StudyError, match="INCOMPLETE_ACCEPTANCE_REQUIRED"):
        service.export(result["id"])
    accepted = service.save_draft(result["id"], result["draft"], reviewed=True, accept_incomplete=True)
    assert accepted["ready_for_export"]


def test_bridge_failure_in_code_generation_keeps_independent_writing(automatic, monkeypatch):
    service = automatic
    service.ai = StructuredAI([
        deliverable("experiment", "Calculate sample measurements", "python",
                    required_capabilities=["python-standard-library"]),
        deliverable("summary", "Summarize the question", "writing"),
    ], {"experiment": AIError("AI_BRIDGE_ERROR"),
        "summary": {"content": "The sample data are supplied by the question.",
                    "sources": [], "required_actions": []}})
    monkeypatch.setattr(service.agent.runner, "status", lambda: {"available": True})
    task = service.complete_assignment("2", "structured-bridge-failure")
    result = await_task(service, task["id"], timeout=15)
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "needs_action"
    assert result["tool_results"]["unit-experiment"]["status"] == "needs_action"
    assert result["tool_results"]["unit-experiment"]["error_code"] == "AI_BRIDGE_ERROR"
    assert result["tool_results"]["unit-summary"]["status"] == "ready_for_review"
    unit_calls = [call["deliverable"]["id"] for call in service.ai.calls[1:]]
    assert unit_calls.count("experiment") > 1 and unit_calls.count("summary") == 1
    assert not result.get("runs")
    assert "Calculate sample measurements" in "\n".join(result["blockers"])
    assert "The sample data are supplied by the question." in result["draft"]


def test_task_budget_preserves_checkpointed_sections_and_lists_pending_work(automatic, monkeypatch):
    service = automatic
    assignment = service.get("assignment", "2")
    assignment.update(asset_ids=[], description="Submit a summary of the source.\nExplain the results.")
    service.store.save(service.user, "assignment", "2", assignment)
    service.ai = StructuredAI([
        deliverable("summary", "Summarize the source", "writing", requirement_ids=["REQ-01"]),
        deliverable("analysis", "Analyze the results", "writing", requirement_ids=["REQ-02"]),
    ], {"summary": {"content": "The first section survived.", "sources": [], "required_actions": []}})
    reserve = service.agent.reserve_request

    def limited(user, task, chars=0):
        if "unit-summary" in task["tool_results"]:
            raise StudyError("PROMPT_BUDGET_EXCEEDED")
        reserve(user, task, chars)

    monkeypatch.setattr(service.agent, "reserve_request", limited)
    task = service.complete_assignment("2", "structured-budget")
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "needs_action"
    assert [item["status"] for item in result["coverage"]] == ["ready_for_review", "needs_action"]
    assert "The first section survived." in result["draft"]
    assert "Analyze the results" in result["completion"]["required_actions"][0]
    assert "PROMPT_BUDGET_EXCEEDED" in result["completion"]["required_actions"][0]
    assert "unit-analysis" not in result["tool_results"]


class RecordedRunner:
    """Record actual input bytes, using deterministic synthetic output instead of host execution."""

    def __init__(self):
        self.calls = []

    def status(self):
        return {"available": True}

    def run(self, code, inputs, directory, cancel, timeout):
        self.calls.append(code)
        run_id = f"synthetic-run-{len(self.calls)}"
        output = directory / run_id
        output.mkdir(parents=True)
        if code == MEASUREMENT_CODE:
            assert MEASUREMENT_INPUT not in inputs
            name, content, exit_code = "measurement.json", MEASUREMENT, 0
        else:
            assert code == VERIFICATION_CODE and inputs[MEASUREMENT_INPUT] == MEASUREMENT
            name, content, exit_code = "verification.json", VERIFICATION, int(len(self.calls) == 2)
        path = output / name
        path.write_bytes(content)
        return {
            "run_id": run_id,
            "image_id": "synthetic-image",
            "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
            "source_hashes": {key: hashlib.sha256(value).hexdigest() for key, value in inputs.items()},
            "exit_code": exit_code,
            "stdout": "synthetic retry" if exit_code else "synthetic validation succeeded",
            "artifacts": {name: {"path": str(path), "sha256": hashlib.sha256(content).hexdigest(),
                                 "bytes": len(content)}},
        }


def test_dependent_units_use_verified_outputs_and_retry_only_failed_unit(automatic):
    service = automatic
    service.ai = StructuredAI(python_plan(), code_responses())
    runner = service.agent.runner = RecordedRunner()
    task = service.complete_assignment("2", "structured-dependency-retry")
    result = await_task(service, task["id"], timeout=15)
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "complete"
    assert runner.calls == [MEASUREMENT_CODE, VERIFICATION_CODE, VERIFICATION_CODE]
    assert result["checkpoint"]["attempts"]["unit-measure"] == 1
    assert result["checkpoint"]["attempts"]["unit-verify"] == 2
    assert result["checkpoint"]["cursor"] == len(result["plan"]["steps"])
    assert digest(result["plan"]) == result["plan_sha256"]
    assert result["tool_results"]["unit-measure"]["status"] == "verified"
    assert result["tool_results"]["unit-measure"]["validation_scope"] == "execution_and_file_format"
    assert result["coverage"][0]["validation_scope"] == "execution_and_file_format"
    assert result["coverage"][0]["status"] == "execution_checked"
    assert "values and method remain subject to review." in result["draft"]
    verified = result["tool_results"]["unit-verify"]
    assert verified["status"] == "verified"
    artifact = verified["artifacts"]["verify__verification.json"]
    assert artifact["filename"] == "verification.json"
    assert filesystem_path(service.safe_path(artifact["path"])).read_bytes() == VERIFICATION
    assert result["runs"][-1]["source_hashes"][MEASUREMENT_INPUT] == MEASUREMENT_SHA256
    assert "Measure sample values" in result["draft"] and "Verify measurements" in result["draft"]
    reviewed = service.save_draft(result["id"], result["draft"], reviewed=True)
    programs = {filesystem_path(service.safe_path(artifact["path"])).read_text(encoding="utf-8")
                for name, artifact in reviewed["generated_files"].items() if name.endswith(".py")}
    assert {MEASUREMENT_CODE, VERIFICATION_CODE} <= programs


def test_parseable_but_wrong_calculation_is_presented_for_value_review(automatic):
    assignment = automatic.get("assignment", "2")
    assignment.update(asset_ids=[], description="Calculate the sum of 20 and 22 and submit measurement.json.")
    automatic.store.save(automatic.user, "assignment", "2", assignment)
    automatic.ai = StructuredAI([
        deliverable("measure", "Calculate the required sum", "python", expected_artifacts=["measurement.json"],
                    verification="Assert that the sum equals 42."),
        deliverable("report", "Explain the recorded output", "writing", dependencies=["measure"]),
    ], {"measure": {"code": MEASUREMENT_CODE},
        "report": {"content": "The run recorded a value of six.", "sources": [], "required_actions": []}})
    automatic.agent.runner = RecordedRunner()
    result = await_task(automatic, automatic.complete_assignment("2", "incorrect-sum")["id"])
    assert result["status"] == "waiting_review"
    assert automatic.ai.calls[-1]["dependencies"][0]["validation_scope"] == "execution_and_file_format"
    assert result["coverage"][0]["validation_scope"] == "execution_and_file_format"
    assert result["coverage"][0]["status"] == "execution_checked"
    assert "The run recorded a value of six." in result["draft"]
    assert "Execution record" not in result["draft"] and "measurement.json" not in result["draft"]


def test_successful_exit_without_required_file_remains_incomplete(automatic):
    class MissingArtifactRunner(RecordedRunner):
        def run(self, *args, **kwargs):
            result = super().run(*args, **kwargs)
            result["artifacts"].clear()
            return result

    service = automatic
    service.ai = StructuredAI(python_plan(), code_responses())
    runner = service.agent.runner = MissingArtifactRunner()
    task = service.complete_assignment("2", "structured-artifact-missing")
    result = await_task(service, task["id"], timeout=15)
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "needs_action"
    assert len(runner.calls) > 1 and set(runner.calls) == {MEASUREMENT_CODE}
    assert result["tool_results"]["unit-measure"]["status"] == "needs_action"
    assert result["tool_results"]["unit-verify"]["status"] == "needs_action"
    assert "measurement.json" in "\n".join(result["tool_results"]["unit-measure"]["reasons"])


@pytest.mark.skipif(os.environ.get("NTPU_TEST_RUNNER") != "1", reason="Requires the configured Docker Runner")
def test_dependent_deliverables_complete_in_real_docker(automatic):
    service = automatic
    plan, responses = python_plan(), code_responses()
    image_file = io.BytesIO()
    Image.new("RGB", (16, 16), "green").save(image_file, format="PNG")
    image = image_file.getvalue()
    plan[0]["expected_artifacts"].append("chart.png")
    responses["verify"]["code"] += f'Path("/output/chart.png").write_bytes({image!r})\n'
    plan.append(deliverable("interpret", "Interpret the verified measurements", "writing", dependencies=["verify"],
                            required_capabilities=["python-standard-library"]))
    responses["interpret"] = {"content": "The verified total is six.", "sources": [], "required_actions": []}
    service.ai = StructuredAI(plan, responses)
    runner = service.agent.runner = DockerRunner(ROOT / ".local/app/runner.json")
    assert runner.status()["available"], "Build the Linux image with scripts/setup_runner.py first"
    task = service.complete_assignment("2", "real-structured-deliverables")
    result = await_task(service, task["id"], timeout=35)
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "complete"
    assert len(result["runs"]) == 2
    assert all(run["exit_code"] == 0 and run["parameters"]["network"] == "none" for run in result["runs"])
    assert result["runs"][-1]["source_hashes"][MEASUREMENT_INPUT] == MEASUREMENT_SHA256
    interpretation = service.ai.calls[-1]["dependencies"][0]
    assert interpretation["status"] == "verified"
    assert any(file.get("excerpt") == VERIFICATION.decode() for file in interpretation["files"])
    assert "The verified total is six." in result["draft"]
    verified = result["tool_results"]["unit-verify"]
    assert verified["status"] == "verified"
    artifact = filesystem_path(service.safe_path(verified["artifacts"]["verify__verification.json"]["path"]))
    assert artifact.read_bytes() == VERIFICATION
    service.save_draft(result["id"], result["draft"], reviewed=True)
    exported = service.export(result["id"])
    assert exported["status"] == "completed"
    report = filesystem_path(service.safe_path(exported["pdf"]))
    manifest = json.loads(report.with_name("manifest.json").read_text(encoding="utf-8"))
    pdf = report.read_bytes()
    assert b"/Subtype /Image" in pdf
    assert manifest["artifact_sha256"] == hashlib.sha256(pdf).hexdigest()
    assert manifest["experiments_executed"]
    assert [(entry["deliverable_id"], entry["status"]) for entry in manifest["coverage"]] == [
        ("measure", "execution_checked"), ("verify", "execution_checked"), ("interpret", "ready_for_review"),
    ]
    exported = export_extra(service, result["id"], "zip")
    assert exported["exports"]["docx"]["images"] == 1
    package = filesystem_path(service.safe_path(exported["exports"]["zip"]["path"]))
    with zipfile.ZipFile(package) as archive:
        name = "artifacts/unit-verify/verify__verification.json"
        assert archive.read(name) == VERIFICATION
        assert archive.read("artifacts/unit-verify/verify__chart.png") == image
        zip_manifest = json.loads(archive.read("manifest.json"))
        assert zip_manifest["artifacts"][name] == hashlib.sha256(VERIFICATION).hexdigest()
        assert zip_manifest["coverage"] == manifest["coverage"]
