"""Direct-completion UI routing with synthetic durable task records."""

import concurrent.futures
import hashlib
import json

import pytest
from test_mvp import service as service
from test_mvp import task_request

from backend.services.study import StudyError
from frontend.gradio_app import build_ui, generation_status


def callbacks(service):
    ui = build_ui(service)
    return ui, {event.fn.__name__: event for event in ui.fns.values() if event.fn}


def completion_task(service, status="waiting_review"):
    task = service.create_task(task_request(service), "completion-ui")
    task["request"]["mode"] = "complete_assignment"
    task.update(status=status, draft="# Completed report\n\nObserved results.",
                completion={"status": "complete", "blockers": []})
    if status == "partial":
        task["error"] = "TOOL_TIMEOUT"
        task["checkpoint"]["consecutive_errors"] = 3
        task["completion"] = {"status": "blocked", "blockers": ["TOOL_TIMEOUT"]}
    service.store.save(service.user, "task", task["id"], task)
    return task


def test_selection_exposes_direct_completion_and_approval_scope(service):
    ui, events = callbacks(service)
    components = ui.config["components"]
    buttons = [item["props"].get("value") for item in components if item["type"] == "button"]
    assert "直接完成作業 →" in buttons
    assert "開始直接完成作業" in buttons
    manual, direct, scope = events["choose_run_style"].fn("complete")
    assert not manual["visible"] and direct["visible"] and scope["visible"]
    descriptions = [item["props"].get("value", "") for item in components if item["type"] == "markdown"]
    assert any("隔離環境" in text and "連續失敗三次" in text and "不會自動繳交" in text
               for text in descriptions)


@pytest.mark.parametrize("status,destination", [("waiting_review", "review"), ("partial", "generate")])
def test_direct_completion_routes_only_success_to_review(service, monkeypatch, status, destination):
    task = completion_task(service, status)
    calls = []

    def complete(identifier, key):
        calls.append((identifier, key))
        return task

    monkeypatch.setattr(service, "complete_assignment", complete, raising=False)
    _, events = callbacks(service)
    event = events["begin_completion"]
    rows = list(event.fn("2"))
    assert all(len(row) == len(event.outputs) for row in rows)
    assert rows[-1][5] == destination
    assert rows[0][2] == task["draft"]
    assert calls[0][0] == "2" and calls[0][1]
    assert not rows[0][4]
    assert not service.ai.calls
    if status == "partial":
        assert "連續失敗 3／3" in generation_status(task)
        assert "已停止自動續跑" in generation_status(task)


def test_reload_polling_restores_completion_without_resending_or_overwriting_review(service):
    task = completion_task(service)
    _, events = callbacks(service)
    event = events["poll_completion"]
    row = event.fn(task["id"], "generate")
    assert len(row) == len(event.outputs)
    assert row[5] == "review" and row[2] == task["draft"]
    assert all(item == {"__type__": "update"} for item in event.fn(task["id"], "review"))
    assert not service.ai.calls


@pytest.mark.parametrize("entry", ["begin_completion", "poll_completion"])
def test_report_with_user_actions_opens_review_without_claiming_full_completion(service, monkeypatch, entry):
    task = completion_task(service)
    action = "Upload the reviewed PDF to E-learning."
    task["draft"] += "\n## Required user actions\n\n- " + action + "\n"
    task["completion"] = {"status": "needs_action", "blockers": [], "required_actions": [action]}
    service.store.save(service.user, "task", task["id"], task)
    monkeypatch.setattr(service, "complete_assignment", lambda *args: task)
    _, events = callbacks(service)
    event = events[entry]
    if entry == "begin_completion":
        row = list(event.fn("2"))[-1]
    else:
        row = event.fn(task["id"], "generate")
    assert row[5] == "review"
    message = generation_status(task)
    assert "已產生作業，請審閱並處理使用者待辦" in message
    assert action in message
    assert "本次自動執行已完成" not in message
    assert not service.ai.calls


@pytest.mark.parametrize("status", ["partial", "blocked_auth", "failed", "cancelled", "running"])
def test_retry_message_only_promises_automatic_resume_while_running(status):
    task = {"status": status, "request": {"mode": "complete_assignment"},
            "checkpoint": {"consecutive_errors": 1}, "error": "COMPLETION_INPUT_REQUIRED"}
    if status == "running":
        task["error"] = None
    message = generation_status(task)
    assert ("系統將從中斷步驟自動續跑" in message) == (status == "running")
    assert ("已停止自動續跑" in message) == (status != "running")


def test_history_keeps_manual_form_valid_for_completion_tasks(service):
    task = completion_task(service)
    _, events = callbacks(service)
    restored = events["open_saved_task"].fn(task["id"])
    assert restored[9] == "report_draft"
    assert restored[11] == task["id"] and restored[13] == task["draft"]
    assert restored[20] == "review"
    assert events["restore_run_style"].fn(task["id"]) == "complete"


@pytest.mark.parametrize("execution_state", ["failed", "succeeded", "legacy_result"])
def test_completion_source_is_downloadable_even_without_a_successful_run(service, execution_state):
    task = completion_task(service)
    code = "print('available source')\n"
    execution = {"code": code}
    if execution_state in {"succeeded", "legacy_result"}:
        execution["result"] = {"exit_code": 0}
    if execution_state == "legacy_result":
        execution.pop("code")
    else:
        execution["failure"] = "PYTHON_EXECUTION_FAILED" if execution_state == "failed" else None
    task["completion_execution"] = execution
    source = service.workspace(service.user, task["id"]) / "work/analysis.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(code, encoding="utf-8")
    service.write_draft(service.user, task)
    generated = task["generated_files"]["analysis.py"]
    assert service.safe_path(generated["path"]) == source
    assert generated["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_completion_source_modified_on_disk_is_not_registered(service):
    task = completion_task(service)
    task["completion_execution"] = {"code": "print('approved')\n", "failure": "PYTHON_EXECUTION_FAILED"}
    source = service.workspace(service.user, task["id"]) / "work/analysis.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("print('changed on disk')\n", encoding="utf-8")
    with pytest.raises(StudyError, match="CODE_CHANGED"):
        service.write_draft(service.user, task)
    assert "analysis.py" not in task["generated_files"]


def test_completion_callback_runs_real_pipeline_without_requiring_unused_sandbox(service, monkeypatch):
    assignment = service.get("assignment", "2")
    assignment.update(asset_ids=[], description="Write a method comparison in English.")
    service.store.save(service.user, "assignment", "2", assignment)
    monkeypatch.setattr(service.agent.runner, "status", lambda: {"available": False})

    def begin(prompt):
        service.ai.calls.append(prompt)
        future = concurrent.futures.Future()
        if '"dataset_catalog"' in prompt:
            result = {"objective": "Compare classification methods", "deliverable_type": "proposal",
                      "dataset_id": None, "needs_execution": False, "research_query": "", "blockers": []}
        else:
            result = {"title": "Method comparison", "sections": [{"heading": "Comparison",
                "content": "Decision trees partition observations by attribute values; nearest neighbors compare distances."}],
                "sources": ["assignment-description"], "unresolved": [],
                "report_labels": {"sources_heading": "Sources"},
                "completion": {"status": "complete", "blockers": []}}
        future.set_result(json.dumps(result))
        return "completion-ui-pipeline", future

    monkeypatch.setattr(service.ai, "begin", begin)
    _, events = callbacks(service)
    event = events["begin_completion"]
    rows = list(event.fn("2"))
    identifier = rows[0][0]
    task = service.get("task", identifier)
    assert task["status"] == "waiting_review", task.get("error")
    assert task["completion"]["status"] == "complete"
    assert rows[-1][5] == "review"
    assert all(len(row) == len(event.outputs) for row in rows)
    assert len(service.ai.calls) == 2
    assert not task.get("reviewed") and not task.get("pdf")
    assert not service.active_jobs
