"""Generation reconnect and explicit recovery, using synthetic AI responses only."""

import concurrent.futures
import time

import gradio as gr
import pytest
from test_mvp import FakeAI, await_task, task_request
from test_mvp import service as service

from backend.p0.ai import AIAdapter, connection_status
from frontend.gradio_app import build_ui


def callbacks(service):
    ui = build_ui(service)
    return {e.fn.__name__: e for e in ui.fns.values() if e.fn}


def test_generation_reconnects_existing_session_without_login(service):
    calls = []
    def rpc(method):
        calls.append(method)
        return {"ready": method == "connect", "authentication": "unknown"}
    service.ai.rpc = rpc
    task = service.create_task({**task_request(service), "data_transfer_approved": True}, "restore-session")
    task = await_task(service, task["id"])
    assert task["status"] == "waiting_review"
    assert calls == ["login_status", "connect"]
    assert len(service.ai.calls) == task["checkpoint"]["requests_used"] == 1


@pytest.mark.parametrize("authentication,expected", [
    ("required", "AI_AUTH_REQUIRED"), ("authenticated", "AI_CONNECTION_PENDING"),
])
def test_unready_connection_does_not_spend_ai_request_budget(service, authentication, expected):
    calls = []
    def rpc(method):
        calls.append(method)
        return {"ready": False, "authentication": authentication, "loginWindow": True}
    service.ai.rpc = rpc
    task = service.create_task({**task_request(service), "data_transfer_approved": True}, "auth-handoff")
    task = await_task(service, task["id"])
    assert task["error"] == expected
    assert calls == ["login_status"]
    assert task["checkpoint"]["requests_used"] == 0
    assert not service.ai.calls


def test_unknown_login_window_is_not_closed_by_reconnect(service):
    calls = []
    def rpc(method):
        calls.append(method)
        return {"ready": False, "authentication": "unknown", "loginWindow": True}
    service.ai.rpc = rpc
    assert not connection_status(service.ai)["ready"]
    assert calls == ["login_status"]


def test_ai_has_separate_time_budget_from_local_tools(service):
    original = service.ai.begin
    def slow(prompt):
        time.sleep(1.1)
        return original(prompt)
    service.ai.begin = slow
    task = service.create_task({**task_request(service), "agent": {"tool_timeout": 1, "ai_timeout": 3},
                               "data_transfer_approved": True}, "separate-timeouts")
    task = await_task(service, task["id"])
    assert task["status"] == "waiting_review"


def test_adapter_propagates_response_timeout_to_browser(monkeypatch):
    adapter = AIAdapter()
    calls = []
    monkeypatch.setattr(adapter, "rpc", lambda method, **params: calls.append((method, params)))
    adapter.begin("synthetic", timeout_seconds=179.5)
    assert calls[0][0] == "start_task"
    assert calls[0][1]["timeout_ms"] == 179500


def test_step_four_resumes_timeout_without_repeating_completed_tools(service):
    service.ai.begin = lambda prompt: ("hanging", concurrent.futures.Future())
    task = service.create_task({**task_request(service), "agent": {"ai_timeout": 1},
                               "data_transfer_approved": True}, "resume-timeout")
    partial = await_task(service, task["id"])
    assert partial["error"] == "TOOL_TIMEOUT"
    ui = callbacks(service)
    status, message, button = ui["refresh_generation"].fn(task["id"])
    assert "不代表需要重新登入" in status and button["interactive"]
    assert "已完成步驟會保留" in message
    assert ui["open_saved_task"].fn(task["id"])[-8] == "generate"
    service.ai.begin = FakeAI.begin.__get__(service.ai)
    event = ui["resume_generation"]
    outputs = list(event.fn(task["id"]))
    assert all(len(row) == len(event.outputs) for row in outputs)
    complete = service.get("task", task["id"])
    assert complete["status"] == "waiting_review" and complete["error"] is None
    assert complete["checkpoint"]["requests_used"] == 2
    assert complete["deadline_at"] == partial["deadline_at"]
    assert sum(a["tool"] == "read_document" for a in complete["tool_audit"]) == 1
    assert len(service.store.list(service.user, "task")) == 1


def test_step_four_expired_task_can_regenerate_with_new_timeout(service):
    request = task_request(service)
    task = service.create_task(request, "expired")
    task.update(status="partial", error="TOOL_TIMEOUT", deadline_at=time.time() - 1)
    service.store.save(service.user, "task", task["id"], task)
    ui = callbacks(service)
    _, message, button = ui["refresh_generation"].fn(task["id"])
    assert "超過總時限" in message and not button["interactive"]
    with pytest.raises(gr.Error, match="重新產生草稿"):
        list(ui["resume_generation"].fn(task["id"]))
    outputs = list(ui["regenerate"].fn(task["id"], "2", request["selected_asset_ids"],
                                     request["mode"], "", request["excerpt"], ""))
    identifier = outputs[0][0]
    assert identifier != task["id"]
    assert service.get("task", identifier)["plan"]["limits"]["ai_timeout"] == 180
    assert service.get("task", task["id"]) == task


def test_step_four_connection_actions_do_not_send_prompts_or_interrupt_generation(service):
    ui = callbacks(service)
    calls = []
    service.ai.rpc = lambda method: calls.append(method) or {"ready": True}
    assert "已連線" in ui["check_generation_ai"].fn()
    assert "已開啟" in ui["login_generation_ai"].fn()
    assert calls == ["login_status", "open_login"]
    assert not service.ai.calls
    service.ai_lock.acquire()
    try:
        assert "仍在執行" in ui["check_generation_ai"].fn()
        assert "仍在執行" in ui["login_generation_ai"].fn()
        assert calls == ["login_status", "open_login"]
    finally:
        service.ai_lock.release()


def test_step_four_waits_for_login_handoff_without_spending_retry(service):
    service.ai.rpc = lambda method: {"ready": False, "authentication": "required"}
    task = service.create_task({**task_request(service), "data_transfer_approved": True}, "wait-for-login")
    task = await_task(service, task["id"])
    service.ai.rpc = lambda method: {"ready": False, "authentication": "authenticated", "loginWindow": True}
    resume = callbacks(service)["resume_generation"].fn
    with pytest.raises(gr.Error, match="交接背景連線"):
        list(resume(task["id"]))
    assert service.get("task", task["id"])["checkpoint"] == task["checkpoint"]
    assert not service.ai.calls and not service.active_jobs
    assert service.ai_lock.acquire(blocking=False)
    service.ai_lock.release()
