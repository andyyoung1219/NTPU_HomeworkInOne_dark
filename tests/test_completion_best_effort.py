"""Generate useful reports despite optional failures without inventing evidence."""

import hashlib

import httpx
import pytest
from test_completion import automatic as automatic
from test_mvp import await_task
from test_mvp import service as service

from backend.agent import completion
from backend.p0.moodle import probe
from backend.services.study import StudyError


def test_model_unresolved_items_become_report_actions_without_retries(automatic):
    def incomplete(payload, model):
        model["completion"] = {"status": "blocked", "blockers": ["Add your student number to the filename."]}
        model["unresolved"] = ["Upload the reviewed PDF to E-learning before the deadline."]
        return model

    automatic.ai.report_factory = incomplete
    task = automatic.complete_assignment("2", "unfinished-delivery")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review" and result["error"] is None
    assert result["completion"]["status"] == "needs_action"
    assert len(automatic.ai.calls) == 2
    assert "## Required user actions" in result["draft"]
    assert "student number" in result["draft"] and "Upload the reviewed PDF" in result["draft"]
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
    assert "Read or convert the unreadable attachment sample.pdf" in result["draft"]


def test_partial_model_action_list_cannot_drop_saved_pending_actions(automatic):
    automatic.ai.plan_blockers = ["Provide the required interview data.", "Provide the sampling records."]

    def one_action(payload, model):
        model["completion"]["required_actions"] = ["Upload the PDF."]
        return model

    automatic.ai.report_factory = one_action
    task = automatic.complete_assignment("2", "all-pending-actions")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review"
    assert all(action in result["draft"] for action in automatic.ai.plan_blockers)
    assert "Upload the PDF." in result["draft"]


def test_pending_action_ids_preserve_translations_in_the_report_language(automatic):
    automatic.ai.plan_blockers = ["Provide the required interview data.", "Provide the sampling records."]

    def translated(payload, model):
        assert payload["pending_actions"] == {"action-1": automatic.ai.plan_blockers[0],
                                              "action-2": automatic.ai.plan_blockers[1]}
        model.update(title="訪談研究報告", sections=[{"heading": "方法", "content": "本研究依題目規劃訪談與抽樣方法；未取得訪談資料，因此本節不報告受訪結果。"}])
        model["report_labels"]["actions_heading"] = "使用者待辦"
        model["completion"]["action_resolutions"] = {"action-1": "補上訪談資料後完成分析。", "action-2": "補上抽樣紀錄。"}
        return model

    automatic.ai.report_factory = translated
    task = automatic.complete_assignment("2", "localized-pending-actions")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review"
    assert "## 使用者待辦" in result["draft"]
    assert "補上訪談資料後完成分析。" in result["draft"] and "補上抽樣紀錄。" in result["draft"]
    assert not any(action in result["draft"] for action in automatic.ai.plan_blockers)


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
    assert "sample.pdf" in result["draft"] and result["completion"]["status"] == "needs_action"


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
    assert "execution has not been verified" in result["draft"]


def test_substantive_report_with_unfinished_section_is_available_with_actions(automatic):
    def incomplete(payload, model):
        model["sections"][0]["content"] += "\n\nAdditional interview findings: TODO."
        return model

    automatic.ai.report_factory = incomplete
    task = automatic.complete_assignment("2", "unfinished-section")
    result = await_task(automatic, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"]["status"] == "needs_action"
    assert "The supplied sources establish" in result["draft"]
    assert "Complete the content marked TODO/TBD" in result["draft"]
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
    assert "Obtain or check the unavailable dataset/references" in result["draft"]
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
    assert "execution has not been verified" in result["draft"]
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
