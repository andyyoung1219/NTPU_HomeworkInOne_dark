"""Synthetic P3 orchestration gate. Docker isolation has a separate real gate."""

import base64
import concurrent.futures
import hashlib
import io
import json
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_mvp import FakeAI, await_task, task_request
from test_mvp import service as service

import backend.agent.runner as runner_module
from backend.agent.contracts import validate_step
from backend.agent.runner import DockerRunner
from backend.main import ORIGIN, create_app
from backend.p0.ai import AIError
from backend.services.study import StudyError, StudyService


class SyntheticRunner:
    """Never execute code; supply independently specified fixture measurements."""

    def __init__(self):
        self.calls = []
        self.wrong_statistics = False

    def status(self):
        return {"available": True}

    def run(self, code, inputs, directory, cancel, timeout):
        self.calls.append({"code": code, "inputs": inputs})
        path = Path(directory) / "synthetic" / "statistics.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"column": "value", "count": 3, "missing": 1,
            "mean": 999 if self.wrong_statistics else 4.0,
            "minimum": 2.0, "maximum": 6.0, "stdev": 2.0}), encoding="utf-8")
        return {"run_id": "synthetic-run", "exit_code": 0, "stdout": "synthetic evidence",
                "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                "source_hashes": {key: hashlib.sha256(value).hexdigest() for key, value in inputs.items()},
                "image_id": "synthetic-only", "parameters": {"timeout": timeout},
                "packages": {"python": "synthetic"}, "artifacts": {
                    "statistics.json": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "path": str(path)}}}


def add_csv(service):
    data = b"name,value\na,2\nb,4\nc,6\nd,\n"
    path = service.root / "fixture.csv"
    path.write_bytes(data)
    ref = "csv-fixture"
    service.store.save(service.user, "asset", ref, {"id": ref, "assignment_id": "2",
        "name": "fixture.csv", "mime_type": "text/csv", "path": "fixture.csv", "status": "parsed",
        "sha256": hashlib.sha256(data).hexdigest()})
    assignment = service.get("assignment", "2")
    assignment["asset_ids"].append(ref)
    service.store.save(service.user, "assignment", "2", assignment)
    return ref


def p3_request(service, mode="report_draft"):
    request = task_request(service)
    request.update(mode=mode, requirements="[ ] 提供來源與結論")
    if mode in {"csv_analysis", "coding_test"}:
        request["allow_code_execution"] = True
        request["agent"] = {"python_code": "assert 2 + 2 == 4\nprint('passed')"}
        if mode == "csv_analysis":
            ref = add_csv(service)
            request["selected_asset_ids"].append(ref)
            request["agent"].update(csv_asset_id=ref, csv_column="value")
    return request


@pytest.mark.parametrize("mode", ["summary", "literature_comparison", "csv_analysis", "coding_test", "report_draft"])
@pytest.mark.parametrize("repetition", range(3))
def test_five_representative_tasks_three_times(service, mode, repetition):
    service.agent.runner = SyntheticRunner()
    task = service.create_task(p3_request(service, mode), f"gate-{mode}-{repetition}")
    assert task["plan"] and not service.ai.calls
    service.approve(task["id"], task["request_sha256"], high=mode in {"csv_analysis", "coding_test"})
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert result["checkpoint"]["cursor"] == len(result["plan"]["steps"])
    assert all(item["success"] for item in result["tool_audit"])
    assert not result["ready_for_export"]  # Semantic requirements still require review.
    if mode == "csv_analysis":
        assert result["statistics_verified"]
        assert "chart.png" in result["draft"]
    if mode in {"coding_test", "csv_analysis"}:
        assert len(service.agent.runner.calls) == 1


def test_code_and_external_approval_are_separate_and_snapshot_bound(service):
    service.agent.runner = SyntheticRunner()
    request = p3_request(service, "coding_test")
    request.update(allow_external_research=True, data_transfer_approved=True)
    request["agent"]["research_query"] = "learning agents"
    task = service.create_task(request, "permissions")
    assert task["status"] == "waiting_approval"
    with pytest.raises(StudyError, match="CODE_APPROVAL_REQUIRED"):
        service.approve(task["id"], task["request_sha256"])
    with pytest.raises(StudyError, match="EXTERNAL_APPROVAL_REQUIRED"):
        service.approve(task["id"], task["request_sha256"], high=True)
    with pytest.raises(StudyError, match="APPROVAL_SNAPSHOT_MISMATCH"):
        service.approve(task["id"], "stale", high=True, external=True)
    assert not service.ai.calls and not service.agent.runner.calls


def test_missing_sandbox_never_falls_back_to_host(service):
    task = service.create_task(p3_request(service, "coding_test"), "no-docker")
    with pytest.raises(StudyError, match="SANDBOX_NOT_AVAILABLE"):
        service.approve(task["id"], task["request_sha256"], high=True)
    assert not service.ai.calls


def test_statistics_are_recomputed_and_fabricated_results_blocked(service):
    service.agent.runner = SyntheticRunner()
    service.agent.runner.wrong_statistics = True
    task = service.create_task(p3_request(service, "csv_analysis"), "fake-numbers")
    service.approve(task["id"], task["request_sha256"], high=True)
    result = await_task(service, task["id"])
    assert result["error"] == "STATISTICS_VERIFICATION_FAILED"
    assert not result["ready_for_export"] and not service.ai.calls


def test_tool_whitelist_schema_and_plan_tampering(service):
    task = service.create_task(p3_request(service), "tamper")
    step = task["plan"]["steps"][0]
    with pytest.raises(StudyError, match="TOOL_NOT_ALLOWED"):
        validate_step({**step, "tool": "submit_to_moodle"})
    with pytest.raises(StudyError, match="TOOL_INPUT_SCHEMA_INVALID"):
        validate_step({**step, "required_inputs": {"path": "../../credentials"}})
    task["plan"]["steps"][0]["tool"] = "run_python"
    service.store.save(service.user, "task", task["id"], task)
    with pytest.raises(StudyError, match="PLAN_CHANGED"):
        service.approve(task["id"], task["request_sha256"])


@pytest.mark.parametrize("code", ["AI_AUTH_REQUIRED", "AI_SERVICE_LIMIT", "RATE_LIMITED", "PERMISSION_DENIED"])
def test_auth_rate_limit_permission_stop_without_retry(service, code):
    def failing(prompt):
        service.ai.calls.append(prompt)
        result = concurrent.futures.Future()
        result.set_exception(AIError(code))
        return "blocked", result
    service.ai.begin = failing
    task = service.create_task({**p3_request(service), "data_transfer_approved": True}, code)
    result = await_task(service, task["id"])
    assert result["error"] == code
    assert len(service.ai.calls) == 1


def test_malformed_reply_retry_is_bounded(service):
    def bad(prompt):
        service.ai.calls.append(prompt)
        result = concurrent.futures.Future()
        result.set_result("not JSON")
        return "bad", result
    service.ai.begin = bad
    task = service.create_task({**p3_request(service), "data_transfer_approved": True}, "bounded")
    result = await_task(service, task["id"])
    assert result["error"] == "AI_INVALID_REPORT_SCHEMA"
    assert len(service.ai.calls) == 2
    service.agent.resume(task["id"], task["request_sha256"])
    result = await_task(service, task["id"])
    assert result["error"] == "RETRY_BUDGET_EXCEEDED"
    assert len(service.ai.calls) == 2


def test_restart_resume_skips_completed_tools_and_keeps_budgets(service):
    service.ai.rpc = lambda *a, **kw: {"ready": False}
    task = service.create_task({**p3_request(service), "data_transfer_approved": True}, "resume")
    partial = await_task(service, task["id"])
    assert partial["status"] == "blocked_auth"
    reopened = StudyService(service.root, FakeAI())
    try:
        assert not reopened.ai.calls
        reopened.agent.resume(task["id"], task["request_sha256"])
        complete = await_task(reopened, task["id"])
        assert complete["status"] == "waiting_review"
        assert complete["checkpoint"]["requests_used"] == 1
        assert sum(a["tool"] == "read_document" for a in complete["tool_audit"]) == 1
    finally:
        reopened.close()


def test_cancel_hanging_model_returns_control(service):
    began = threading.Event()
    future = concurrent.futures.Future()
    def hanging(prompt):
        began.set()
        return "hanging", future
    service.ai.begin = hanging
    task = service.create_task({**p3_request(service), "data_transfer_approved": True}, "cancel")
    assert began.wait(timeout=5)
    service.cancel(task["id"])
    result = await_task(service, task["id"])
    assert result["status"] == "cancelled"
    assert not service.active_jobs


@pytest.mark.parametrize("option,value,error", [("max_steps", 1, "STEP_BUDGET_EXCEEDED"),
    ("max_prompt_chars", 1000, "PROMPT_BUDGET_EXCEEDED")])
def test_limits_bound_execution(service, option, value, error):
    task = service.create_task({**p3_request(service), "agent": {option: value},
                               "data_transfer_approved": True}, option)
    assert await_task(service, task["id"])["error"] == error


def test_expired_task_does_not_gain_new_deadline(service):
    task = service.create_task(p3_request(service), "expired")
    task.update(status="partial", deadline_at=time.time() - 1)
    service.store.save(service.user, "task", task["id"], task)
    with pytest.raises(StudyError, match="TASK_DEADLINE_EXCEEDED"):
        service.agent.resume(task["id"], task["request_sha256"])
    assert not service.ai.calls


def test_material_selection_cannot_cross_courses(service):
    service.store.save(service.user, "material", "foreign", {"id": "foreign", "course_id": "999"})
    with pytest.raises(StudyError, match="MATERIAL_SCOPE_MISMATCH"):
        service.create_task({**p3_request(service), "agent": {"selected_material_ids": ["foreign"]}}, "scope")


def test_review_requires_explicit_acceptance_and_export_contains_evidence(service):
    service.agent.runner = SyntheticRunner()
    task = service.create_task(p3_request(service, "csv_analysis"), "export")
    service.approve(task["id"], task["request_sha256"], high=True)
    result = await_task(service, task["id"])
    service.save_draft(task["id"], result["draft"], reviewed=True)
    with pytest.raises(StudyError, match="INCOMPLETE_ACCEPTANCE_REQUIRED"):
        service.export(task["id"])
    service.save_draft(task["id"], result["draft"], reviewed=True, accept_incomplete=True)
    exported = service.export(task["id"])
    import pymupdf
    with pymupdf.open(service.safe_path(exported["pdf"])) as pdf:
        assert any(page.get_images() for page in pdf)
    manifest = json.loads((service.workspace(service.user, task["id"]) / "output/manifest.json").read_text(encoding="utf-8"))
    assert manifest["statistics_verified"] and manifest["runs"][0]["run_id"] == "synthetic-run"
    assert manifest["accepted_incomplete"]


def test_api_rejects_bad_options_and_scopes_artifacts(service):
    with TestClient(create_app(service, "p3-local"), base_url=ORIGIN) as client:
        client.get("/unlock/p3-local")
        response = client.post("/api/tasks", headers={"Idempotency-Key": "bad"},
                               json={**p3_request(service), "agent": {"shell": "anything"}})
        assert response.status_code == 422
        assert "submit_to_moodle" not in client.get("/api/tools").json()["data"]["registry"]
        assert client.get("/api/tasks/foreign/artifacts/analysis.py").status_code == 404


def test_runner_command_has_no_secret_or_writable_host_mount(tmp_path):
    command = DockerRunner.command("sha256:" + "a" * 64, "ntpu-synthetic", tmp_path)
    assert "--network=none" in command and "--read-only" in command
    assert "--user=65532:65532" in command and "--cap-drop=ALL" in command
    mount = command[command.index("--mount") + 1]
    assert mount.endswith("target=/input,readonly")
    assert command.count("--mount") == 1
    assert not any(item in command for item in ("--privileged", "--env", "-v", "--gpus", "sh"))


@pytest.mark.parametrize("budget,expected", [(1, "REQUEST_BUDGET_EXCEEDED"), (2, None)])
def test_research_query_scope_and_request_budget(service, monkeypatch, budget, expected):
    client_type = httpx.Client
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.host == "api.crossref.org"
        assert request.url.params["query.bibliographic"] == "approved query"
        assert not request.headers.get("cookie") and not request.headers.get("authorization")
        return httpx.Response(200, json={"message": {"items": [
            {"DOI": "10.1234/synthetic", "title": ["Synthetic metadata only"]}]}})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_type(**kwargs, transport=httpx.MockTransport(handler)))
    task = service.create_task({**p3_request(service), "allow_external_research": True,
        "agent": {"research_query": "approved query", "max_requests": budget}}, "research-budget")
    service.approve(task["id"], task["request_sha256"], external=True)
    result = await_task(service, task["id"])
    assert result["error"] == expected
    assert len(calls) == 1
    assert len(service.ai.calls) == (1 if expected is None else 0)
    assert any(ref.startswith("crossref:") for ref in result["source_hashes"])


def test_tool_timeout_does_not_retry_uncertain_ai_transmission(service):
    service.ai.begin = lambda prompt: ("never-finishes", concurrent.futures.Future())
    task = service.create_task({**p3_request(service), "agent": {"ai_timeout": 1},
                               "data_transfer_approved": True}, "timeout")
    result = await_task(service, task["id"])
    assert result["error"] == "TOOL_TIMEOUT"
    assert result["checkpoint"]["requests_used"] == 1


def test_audit_has_no_document_or_raw_error_secrets(service):
    def fail_with_secret(prompt):
        future = concurrent.futures.Future()
        future.set_exception(AIError("raw Cookie=synthetic-sensitive-value"))
        return "bad", future
    service.ai.begin = fail_with_secret
    task = service.create_task({**p3_request(service), "data_transfer_approved": True}, "redacted")
    result = await_task(service, task["id"])
    assert result["error"] == "AI_BRIDGE_ERROR"
    audit = json.dumps(result["tool_audit"])
    assert "synthetic-sensitive-value" not in audit
    assert task["request"]["excerpt"] not in audit


@pytest.mark.parametrize("name,valid", [("output.txt", True), ("../escape.txt", False), ("payload.exe", False)])
def test_runner_output_protocol_and_cleanup(tmp_path, monkeypatch, name, valid):
    runner = DockerRunner(tmp_path / "unused.json")
    monkeypatch.setattr(runner, "image", lambda: "sha256:" + "1" * 64)
    monkeypatch.setattr(runner, "engine_command", lambda: ["docker"])
    cleaned = []
    payload = {"exit_code": 0, "stdout": "fixture", "packages": {"python": "fixture"},
               "files": {name: base64.b64encode(b"fixture output").decode()}}
    class Process:
        returncode = 0
        stdout = io.BytesIO(json.dumps(payload).encode())
        def poll(self):
            return 0
        def wait(self, timeout):
            return 0
    monkeypatch.setattr(runner_module.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(runner_module.subprocess, "run", lambda args, **kwargs: cleaned.append(args))
    if valid:
        result = runner.run("raise AssertionError('must not run on host')", {}, tmp_path / "runs", threading.Event(), 5)
        assert set(result["artifacts"]) == {"output.txt", "run.json"}
        receipt = Path(result["artifacts"]["run.json"]["path"])
        assert json.loads(receipt.read_text())["image_id"] == "sha256:" + "1" * 64
    else:
        with pytest.raises(StudyError, match="RUNNER_RESULT_INVALID"):
            runner.run("pass", {}, tmp_path / "runs", threading.Event(), 5)
    assert cleaned[0][:3] == ["docker", "rm", "-f"]


def test_editing_partial_draft_keeps_checkpoint_resumable(service):
    service.ai.rpc = lambda *args, **kwargs: {"ready": False}
    task = service.create_task({**p3_request(service), "data_transfer_approved": True}, "partial-edit")
    partial = await_task(service, task["id"])
    edited = service.save_draft(task["id"], partial["draft"] + "\n人工筆記")
    assert edited["status"] == "blocked_auth"
    assert edited["checkpoint"] == partial["checkpoint"]
