"""Checkpoint retry behavior for the opt-in automatic completion mode."""

import threading
import time

import pytest
from test_mvp import service as service
from test_mvp import task_request

from backend.agent.contracts import digest
from backend.services.study import StudyError


class ImmediateCancelEvent(threading.Event):
    """Avoid wall-clock backoff while preserving cancellation semantics."""

    def wait(self, timeout=None):
        return super().wait(0)


@pytest.fixture
def execution(service):
    task = service.create_task(task_request(service), "completion-retries")
    task["request"]["mode"] = "complete_assignment"
    task["plan"]["limits"].update(max_retries=2, max_steps=24)
    task["plan_sha256"] = digest(task["plan"])
    task["deadline_at"] = time.time() + 60
    task["approvals"] = {}
    service.active_jobs[task["id"]] = {"cancel": ImmediateCancelEvent(), "bridge_id": None}
    state = {"user": service.user, "task": task, "done": False}
    yield service, task, state
    service.active_jobs.pop(task["id"], None)


def test_timeouts_retry_checkpoint_without_replaying_completed_steps(execution, monkeypatch):
    service, task, state = execution
    calls = []

    def tool(user, current, step):
        calls.append(step["tool"])
        if step["tool"] == "generate_report" and calls.count("generate_report") < 3:
            raise StudyError("TOOL_TIMEOUT")
        return {"result": step["tool"]}

    monkeypatch.setattr(service.agent, "call_tool", tool)
    while not state["done"]:
        state.update(service.agent.execute(state))
        # Reload durable state between attempts, as a resumed task would.
        state["task"] = service.get("task", task["id"])
    checkpoint = state["task"]["checkpoint"]
    assert calls == ["read_assignment", "read_document", "generate_report", "generate_report", "generate_report"]
    assert checkpoint["cursor"] == 3 and checkpoint["consecutive_errors"] == 0
    assert checkpoint["attempts"]["s3"] == 3
    retries = [event for event in service.store.events(service.user, task["id"])
               if event["event"] == "tool_retry"]
    assert [event["code"] for event in retries] == ["TOOL_TIMEOUT", "TOOL_TIMEOUT"]


def test_third_consecutive_error_stops_and_preserves_failure(execution, monkeypatch):
    service, task, state = execution
    failures = iter(["NETWORK_UNAVAILABLE", "TOOL_TIMEOUT", "OPERATION_TIMEOUT"])

    def tool(*args):
        raise StudyError(next(failures))

    monkeypatch.setattr(service.agent, "call_tool", tool)
    service.agent.execute(state)
    service.agent.execute(state)
    with pytest.raises(StudyError, match="OPERATION_TIMEOUT"):
        service.agent.execute(state)
    saved = service.get("task", task["id"])
    assert saved["checkpoint"]["cursor"] == 0
    assert saved["checkpoint"]["consecutive_errors"] == 3
    assert len(saved["tool_audit"]) == 3
    assert saved["tool_audit"][-1]["error_code"] == "OPERATION_TIMEOUT"


def test_success_resets_failure_streak_for_next_step(execution, monkeypatch):
    service, task, state = execution
    attempts = {}

    def tool(user, current, step):
        attempts[step["id"]] = attempts.get(step["id"], 0) + 1
        if attempts[step["id"]] < 3:
            raise StudyError("TOOL_TIMEOUT")
        return {"result": "completed"}

    monkeypatch.setattr(service.agent, "call_tool", tool)
    while not state["done"]:
        state.update(service.agent.execute(state))
    assert attempts == {"s1": 3, "s2": 3, "s3": 3}
    assert task["checkpoint"]["consecutive_errors"] == 0
    assert task["checkpoint"]["cursor"] == 3


def test_manual_mode_timeouts_still_require_explicit_resume(execution, monkeypatch):
    service, task, state = execution
    task["request"]["mode"] = "report_draft"

    def tool(*args):
        raise StudyError("TOOL_TIMEOUT")

    monkeypatch.setattr(service.agent, "call_tool", tool)
    with pytest.raises(StudyError, match="TOOL_TIMEOUT"):
        service.agent.execute(state)
    assert len(task["tool_audit"]) == 1
    assert "consecutive_errors" not in task["checkpoint"]


@pytest.mark.parametrize("code", [
    "AI_INVALID_COMPLETION_PLAN", "PYTHON_EXECUTION_FAILED", "COMPLETION_INCOMPLETE",
])
def test_completion_stage_failures_retry_without_advancing_checkpoint(execution, monkeypatch, code):
    service, task, state = execution
    attempts = []

    def tool(user, current, step):
        attempts.append(step["id"])
        if len(attempts) == 1:
            raise StudyError(code)
        return {"result": "completed"}

    monkeypatch.setattr(service.agent, "call_tool", tool)
    service.agent.execute(state)
    assert task["checkpoint"]["cursor"] == 0
    assert task["checkpoint"]["consecutive_errors"] == 1
    service.agent.execute(state)
    assert attempts == ["s1", "s1"]
    assert task["checkpoint"]["cursor"] == 1
    assert task["checkpoint"]["consecutive_errors"] == 0


@pytest.mark.parametrize("code", [
    "AI_AUTH_REQUIRED", "RATE_LIMITED", "PERMISSION_DENIED", "FORBIDDEN",
    "CANCELLED", "TASK_DEADLINE_EXCEEDED", "REQUEST_BUDGET_EXCEEDED",
])
def test_safety_and_budget_failures_stop_immediately(execution, monkeypatch, code):
    service, task, state = execution

    def tool(*args):
        raise StudyError(code)

    monkeypatch.setattr(service.agent, "call_tool", tool)
    with pytest.raises(StudyError, match=code):
        service.agent.execute(state)
    assert len(task["tool_audit"]) == 1
    assert not [event for event in service.store.events(service.user, task["id"])
                if event["event"] == "tool_retry"]


def test_cancel_interrupts_automatic_retry_backoff(execution, monkeypatch):
    service, task, state = execution
    waiting = threading.Event()
    errors = []

    class NotifyingCancelEvent(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    cancel = NotifyingCancelEvent()
    service.active_jobs[task["id"]]["cancel"] = cancel

    def tool(*args):
        raise StudyError("TOOL_TIMEOUT")

    def execute():
        try:
            service.agent.execute(state)
        except StudyError as exc:
            errors.append(exc.code)

    monkeypatch.setattr(service.agent, "call_tool", tool)
    worker = threading.Thread(target=execute)
    worker.start()
    assert waiting.wait(2)
    cancel.set()
    worker.join(timeout=0.5)
    assert not worker.is_alive()
    assert errors == ["CANCELLED"]
    assert len(task["tool_audit"]) == 1
