"""Generate useful reports despite optional failures without inventing evidence."""

import hashlib
import json

import httpx
import pytest
from test_completion import automatic as automatic
from test_mvp import await_task
from test_mvp import service as service

from backend.agent import completion
from backend.p0.ai import AIError
from backend.p0.moodle import probe
from backend.services.study import StudyError


def test_ungrounded_model_actions_are_ignored_without_retries(automatic):
    def incomplete(payload, model):
        model["completion"] = {"status": "blocked", "blockers": ["Add your student number to the filename."]}
        model["unresolved"] = ["Upload the reviewed PDF to E-learning before the deadline."]
        return model

    automatic.ai.report_factory = incomplete
    task = automatic.complete_assignment("2", "unfinished-delivery")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review" and result["error"] is None
    assert result["completion"]["status"] == "complete"
    assert len(automatic.ai.calls) == 2
    assert "## Required user actions" not in result["draft"]
    assert "student number" not in result["draft"] and "Upload the reviewed PDF" not in result["draft"]
    assert result["checkpoint"]["consecutive_errors"] == 0


@pytest.mark.parametrize("code", ["pdf_requires_ocr", "UNSUPPORTED_FORMAT", "NETWORK_UNAVAILABLE"])
def test_unreadable_attachment_keeps_usable_assignment_text(automatic, monkeypatch, code):
    def download(*args):
        raise probe.ProbeError(code)

    monkeypatch.setattr(automatic, "download", download)
    task = automatic.complete_assignment("2", "partial-input")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review"
    assert result["completion_inputs"]["assets"] == []
    assert set(result["source_hashes"]) == {"assignment-description"}
    assert "Read or convert the unreadable attachment sample.pdf" in "\n".join(result["completion"]["required_actions"])


def test_partial_model_action_list_cannot_drop_saved_pending_actions(automatic):
    assignment = automatic.get("assignment", "2")
    requirements = ["Provide the required interview data.", "Provide the sampling records.", "Submit the PDF."]
    assignment.update(asset_ids=[], description="\n".join(requirements))
    automatic.store.save(automatic.user, "assignment", "2", assignment)
    automatic.ai.plan_blocker_requirements = [
        {"requirement_id": f"REQ-{index + 1:02}", "quote": requirement}
        for index, requirement in enumerate(requirements[:2])
    ]

    def one_action(payload, model):
        model["completion"]["requirement_actions"] = [{"requirement_id": "REQ-03", "quote": requirements[2]}]
        return model

    automatic.ai.report_factory = one_action
    task = automatic.complete_assignment("2", "all-pending-actions")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review"
    actions = "\n".join(result["completion"]["required_actions"])
    assert all(requirement in actions for requirement in requirements)


def test_pending_action_ids_preserve_translations_in_the_report_language(automatic):
    assignment = automatic.get("assignment", "2")
    requirements = ["Provide the required interview data.", "Provide the sampling records."]
    assignment.update(asset_ids=[], description="\n".join(requirements))
    automatic.store.save(automatic.user, "assignment", "2", assignment)
    automatic.ai.plan_blocker_requirements = [
        {"requirement_id": f"REQ-{index + 1:02}", "quote": requirement}
        for index, requirement in enumerate(requirements)
    ]

    def translated(payload, model):
        assert payload["pending_actions"] == {f"action-{index + 1}": f"請完成題目要求（REQ-{index + 1:02}）。{requirement}"
                                              for index, requirement in enumerate(requirements)}
        model.update(title="訪談研究報告", sections=[{"heading": "方法", "content": "本研究依題目規劃訪談與抽樣方法；未取得訪談資料，因此本節不報告受訪結果。"}])
        model["report_labels"]["actions_heading"] = "使用者待辦"
        model["completion"]["action_resolutions"] = {"action-1": "補上訪談資料後完成分析。", "action-2": "補上抽樣紀錄。"}
        return model

    automatic.ai.report_factory = translated
    task = automatic.complete_assignment("2", "localized-pending-actions")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review"
    assert "## 使用者待辦" not in result["draft"]
    assert result["completion"]["required_actions"] == ["補上訪談資料後完成分析。", "補上抽樣紀錄。"]
    assert not any(requirement in result["draft"] for requirement in requirements)


def test_single_report_only_lists_source_grounded_mandatory_actions(automatic):
    assignment = automatic.get("assignment", "2")
    assignment.update(asset_ids=[], description=(
        "You must submit a signed PDF.\n"
        "Optional: submit a chart for extra credit."
    ))
    automatic.store.save(automatic.user, "assignment", "2", assignment)

    def report(payload, model):
        model["completion"].update(required_actions=["Calculate a new error metric."], requirement_actions=[
            {"requirement_id": "REQ-01", "quote": "You must submit a signed PDF."},
            {"requirement_id": "REQ-02", "quote": "submit a chart for extra credit"},
        ])
        model["sections"][0]["content"] += " A chart is an optional extension."
        return model

    automatic.ai.report_factory = report
    task = automatic.complete_assignment("2", "single-report-grounded-actions")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert len(result["completion"]["required_actions"]) == 1
    assert "REQ-01" in result["completion"]["required_actions"][0]
    assert "A chart is an optional extension." in result["draft"]
    assert "Calculate a new error metric." not in result["draft"]


def test_legacy_proposal_with_malformed_writer_uses_local_snapshot(automatic):
    source = "e" * 24
    dataset = b"group,value\nA,1\nB,3\n"
    (automatic.root / "data.csv").write_bytes(dataset)
    assignment = automatic.get("assignment", "2")
    assignment.update(asset_ids=[source], description="Make a proposal for the data mining project.")
    automatic.store.save(automatic.user, "assignment", "2", assignment)
    automatic.store.save(automatic.user, "asset", source, {
        "id": source, "assignment_id": "2", "name": "data.csv", "mime_type": "text/csv", "path": "data.csv",
        "status": "downloaded", "sha256": hashlib.sha256(dataset).hexdigest(),
    })
    automatic.ai.report_factory = lambda payload, model: {"sections": [{"content": False}]}
    automatic.ai.plan_options = {"deliverable_type": "proposal", "report_language": "en"}
    task = await_task(automatic, automatic.complete_assignment("2", "legacy-proposal-recovery")["id"], timeout=15)
    assert task["status"] == "waiting_review", task.get("error")
    assert task["completion"]["status"] == "needs_action"
    assert task["completion"]["review_required"]
    assert "2 records and 2 columns" in task["draft"]


def test_no_readable_assignment_source_stops_before_ai_generation(automatic, monkeypatch):
    assignment = automatic.get("assignment", "2")
    assignment["description"] = ""
    automatic.store.save(automatic.user, "assignment", "2", assignment)

    def download(*args):
        raise probe.ProbeError("pdf_requires_ocr")

    monkeypatch.setattr(automatic, "download", download)
    task = automatic.complete_assignment("2", "no-input")
    result = await_task(automatic, task["id"])
    assert result["status"] == "partial" and result["error"] == "ASSIGNMENT_TEXT_NOT_AVAILABLE"
    assert not automatic.ai.calls
    assert result["completion"]["status"] == "blocked"


def test_oversized_attachment_is_excluded_without_losing_assignment_text(automatic, monkeypatch):
    asset = automatic.get("asset", "a" * 24)
    automatic.safe_path(asset["path"]).write_bytes(b"x" * 10_000_001)
    monkeypatch.setattr(automatic, "download", lambda *args: None)
    task = automatic.complete_assignment("2", "large-input")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion_inputs"]["assets"] == []
    assert result["completion_issues"][0]["code"] == "INPUT_SIZE_INVALID"
    assert "sample.pdf" in "\n".join(result["completion"]["required_actions"])
    assert result["completion"]["status"] == "needs_action"


@pytest.mark.parametrize("status,code", [(401, "AUTH_REQUIRED"), (403, "PERMISSION_DENIED"), (429, "RATE_LIMITED")])
def test_attachment_auth_and_rate_limit_errors_stop_before_ai(automatic, monkeypatch, status, code):
    def download(*args):
        raise probe.ProbeError("attachment_http_error", status)

    monkeypatch.setattr(automatic, "download", download)
    task = automatic.complete_assignment("2", "attachment-http-" + str(status))
    result = await_task(automatic, task["id"])
    assert result["error"] == code and result["completion"]["status"] == "blocked"
    assert not automatic.ai.calls


@pytest.mark.parametrize("code", ["SANDBOX_EXECUTION_FAILED", "RUNNER_OUTPUT_LIMIT", "RUNNER_RESULT_INVALID"])
def test_runner_failures_keep_report_without_trusting_output(automatic, code):
    automatic.ai.execution = True

    class UnavailableOutputRunner:
        def status(self):
            return {"available": True}

        def run(self, *args):
            raise StudyError(code)

    automatic.agent.runner = UnavailableOutputRunner()
    task = automatic.complete_assignment("2", "runner-" + code)
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review" and result["completion"]["status"] == "needs_action"
    assert not result.get("runs") and not result["completion_execution"].get("result")
    assert "execution has not been verified" in "\n".join(result["completion"]["required_actions"])


@pytest.mark.parametrize("code", ["INCOMPLETE_MODEL_RESPONSE", "EMPTY_MODEL_RESPONSE"])
def test_missing_model_code_continues_to_report_after_bounded_retries(automatic, monkeypatch, code):
    automatic.ai.execution = True
    original_begin = automatic.ai.begin

    def begin(prompt):
        payload = json.loads(prompt.rsplit("\n", 1)[1])
        if "previous_code" in payload:
            raise AIError(code)
        return original_begin(prompt)

    monkeypatch.setattr(automatic.ai, "begin", begin)
    task = automatic.complete_assignment("2", "missing-code-" + code)
    result = await_task(automatic, task["id"], timeout=10)
    assert result["status"] == "waiting_review" and result["completion"]["status"] == "needs_action"
    assert not result.get("runs") and not result["completion_execution"].get("code")
    assert "no executable program was available" in "\n".join(result["completion"]["required_actions"])
    attempts = [step for step in result["tool_audit"] if step["tool"] == "execute_completion"]
    assert len(attempts) == 3 and attempts[-1]["warning_code"] == code


def test_substantive_report_with_unfinished_section_is_available_with_actions(automatic):
    requirement = "You must include the interview findings."
    assignment = automatic.get("assignment", "2")
    assignment.update(asset_ids=[], description=requirement)
    automatic.store.save(automatic.user, "assignment", "2", assignment)

    def incomplete(payload, model):
        model["sections"][0]["content"] += "\n\nAdditional interview findings: TODO."
        model["completion"]["requirement_actions"] = [{"requirement_id": "REQ-01", "quote": requirement}]
        return model

    automatic.ai.report_factory = incomplete
    task = automatic.complete_assignment("2", "unfinished-section")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "needs_action"
    assert "The supplied sources establish" in result["draft"]
    action, = result["completion"]["required_actions"]
    assert "REQ-01" in action and requirement in action
    assert action in result["completion"]["required_actions"]
    assert len(automatic.ai.calls) == 2


def test_dataset_failure_retries_then_generates_without_claiming_statistics(automatic, monkeypatch):
    automatic.ai.dataset_id = "wine-red"
    requests = []
    original_client = httpx.Client

    def unavailable(request):
        requests.append(str(request.url))
        return httpx.Response(503)

    monkeypatch.setattr(completion.httpx, "Client", lambda **kwargs:
                        original_client(transport=httpx.MockTransport(unavailable), **kwargs))
    task = automatic.complete_assignment("2", "dataset-unavailable")
    result = await_task(automatic, task["id"], timeout=10)
    assert result["status"] == "waiting_review", result.get("error")
    assert len(requests) == 3 and result["completion_data"]["datasets"] == []
    assert "Obtain or check the unavailable dataset/references" in "\n".join(result["completion"]["required_actions"])
    audit = [a for a in result["tool_audit"] if a["tool"] == "gather_completion_data"]
    assert len(audit) == 3 and all(not a["success"] for a in audit)
    assert audit[-1]["warning_code"] == "DATASET_UNAVAILABLE"
    assert result["checkpoint"]["requests_used"] == 5  # Plan, 3 fetches, report.


def test_failed_program_remains_downloadable_and_results_are_marked_unverified(automatic):
    automatic.ai.execution = True
    calls = []

    class FailingRunner:
        def status(self):
            return {"available": True}

        def run(self, code, inputs, directory, cancel, timeout):
            calls.append(code)
            return {"exit_code": 1, "stdout": "Assertion failed", "artifacts": {},
                    "run_id": "synthetic-failure", "code_sha256": hashlib.sha256(code.encode()).hexdigest()}

    automatic.agent.runner = FailingRunner()
    task = automatic.complete_assignment("2", "failed-program")
    result = await_task(automatic, task["id"], timeout=10)
    assert result["status"] == "waiting_review", result.get("error")
    assert len(calls) == 3 and not result["completion_execution"].get("result")
    assert "execution has not been verified" in "\n".join(result["completion"]["required_actions"])
    program = automatic.safe_path(result["generated_files"]["analysis.py"]["path"])
    assert program.read_text(encoding="utf-8") == calls[-1]
    assert result["completion"]["status"] == "needs_action"


def test_repaired_execution_does_not_send_stale_failure_to_report(automatic):
    automatic.ai.execution = True
    attempts = []

    class RepairedRunner:
        def status(self):
            return {"available": True}

        def run(self, code, inputs, directory, cancel, timeout):
            attempts.append(code)
            return {"exit_code": 1 if len(attempts) == 1 else 0, "stdout": "verified" if len(attempts) > 1 else "failed",
                    "artifacts": {}, "run_id": "synthetic-repair", "code_sha256": hashlib.sha256(code.encode()).hexdigest()}

    def report(payload, model):
        assert payload["execution"]["exit_code"] == 0
        assert payload["execution_failure"] is None
        assert payload["generated_program"] == attempts[-1]
        return model

    automatic.agent.runner = RepairedRunner()
    automatic.ai.report_factory = report
    task = automatic.complete_assignment("2", "repaired-program")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review" and result["completion"]["status"] == "complete"
    assert len(attempts) == 2


@pytest.mark.parametrize("code", ["ASSET_CHANGED", "PATH_OUTSIDE_WORKSPACE", "AI_AUTH_REQUIRED", "CANCELLED"])
def test_integrity_authorization_and_cancellation_never_degrade_to_success(automatic, monkeypatch, code):
    original_call = automatic.agent.call_tool

    def tool(user, task, step):
        if step["tool"] == "gather_completion_data":
            raise StudyError(code)
        return original_call(user, task, step)

    monkeypatch.setattr(automatic.agent, "call_tool", tool)
    task = automatic.complete_assignment("2", "fatal-" + code)
    result = await_task(automatic, task["id"])
    assert result["status"] in {"partial", "blocked_auth", "cancelled"}
    assert result["error"] == code and result["completion"]["status"] == "blocked"
    assert len(automatic.ai.calls) == 1
