"""Direct-completion UI routing with synthetic durable task records."""

import concurrent.futures
import json

import pytest
from test_mvp import service as service
from test_mvp import task_request

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


def test_history_keeps_manual_form_valid_for_completion_tasks(service):
    task = completion_task(service)
    _, events = callbacks(service)
    restored = events["open_saved_task"].fn(task["id"])
    assert restored[9] == "report_draft"
    assert restored[11] == task["id"] and restored[13] == task["draft"]
    assert restored[20] == "review"
    assert events["restore_run_style"].fn(task["id"]) == "complete"


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
