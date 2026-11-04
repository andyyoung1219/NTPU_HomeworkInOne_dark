import concurrent.futures
import json

import gradio as gr
import pytest
from fastapi.testclient import TestClient
from test_mvp import await_task, task_request
from test_mvp import service as service

from backend.main import ORIGIN, create_app
from backend.services.study import WORKFLOW_MODES, StudyError
from frontend.connections import check_connections
from frontend.gradio_app import build_ui


@pytest.mark.parametrize("mode", WORKFLOW_MODES)
def test_four_modes_generate_reviewable_files(service, mode):
    def begin(prompt):
        service.ai.calls.append(prompt)
        future = concurrent.futures.Future()
        future.set_result(json.dumps({"title": "作業草稿", "sections": [{"heading": "解題",
            "content": "說明\n\n```python\ndef solve():\n    return 42\n```\n"}],
            "sources": ["a" * 24], "unresolved": ["測試待本人執行"]}))
        return "workflow", future
    service.ai.begin = begin
    task = service.create_task({**task_request(service), "mode": mode,
                               "data_transfer_approved": True}, mode)
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert not any(step["tool"] == "run_python" for step in result["plan"]["steps"])
    assert (service.workspace(service.user, task["id"]) / "work/report.md").exists()
    if mode == "code_solution":
        source = service.safe_path(result["generated_files"]["solution_1.py"]["path"])
        assert source.read_text() == "def solve():\n    return 42\n"
        assert "mode_guidance" in service.ai.calls[0]
        with TestClient(create_app(service, "workflow"), base_url=ORIGIN) as client:
            client.get("/unlock/workflow")
            url = f"/api/tasks/{task['id']}/artifacts/solution_1.py"
            assert "return 42" in client.get(url).text
            updated = result["draft"].replace("return 42", "return 43")
            service.save_draft(task["id"], updated)
            assert "return 43" in client.get(url).text
            service.save_draft(task["id"], updated, reviewed=True, accept_incomplete=True)
            exported = service.export(task["id"])
            assert exported["status"] == "completed"
            manifest = client.get(f"/api/artifacts/{task['id']}/download?kind=manifest").json()
            assert "solution_1.py" in manifest["generated_files"]
            assert "return 43" in client.get(url).text
            service.save_draft(task["id"], "# 說明\n已移除程式")
            assert client.get(url).status_code == 400


def test_ui_has_two_centers_and_separate_workflow_screens(service):
    ui = build_ui(service)
    components = ui.config["components"]
    labels = [c["props"].get("label") for c in components]
    buttons = [c["props"].get("value") for c in components if c["type"] == "button"]
    assert "進入學習資料中心 →" in buttons
    assert "進入懶人作業中心 →" in buttons
    pages = [c for c in components if "app-page" in (c["props"].get("elem_classes") or [])]
    assert len(pages) == 7
    assert [c["props"]["elem_id"] for c in pages if c["props"]["visible"]] == ["page-home"]
    assert next(c for c in components if c["props"].get("label") == "帳號")["props"]["type"] == "text"
    assert next(c for c in components if c["props"].get("label") == "密碼")["props"]["type"] == "password"
    assert not next(c for c in components if c["props"].get("elem_id") == "connection-modal")["props"]["visible"]
    assert "工具計畫與續跑" not in labels and "Agent 工作區" not in labels
    radio = next(c for c in components if c["props"].get("label") == "處理方式")
    assert [choice[1] for choice in radio["props"]["choices"]] == list(WORKFLOW_MODES)


def test_wizard_guards_preserve_work_until_inputs_are_ready(service):
    ui = build_ui(service)
    callbacks = {event.fn.__name__: event.fn for event in ui.fns.values() if event.fn}
    with pytest.raises(gr.Error):
        callbacks["require_assignment"](None)
    with pytest.raises(gr.Error):
        callbacks["require_sources"]("")
    with pytest.raises(gr.Error):
        callbacks["validate_start"]("2", "題目", False)
    assert service.store.list(service.user, "task") == []
    callbacks["require_assignment"]("2")
    callbacks["require_sources"]("已讀取的文字")
    callbacks["validate_start"]("2", "題目", True)
    # Failed login and status-only checks must leave in-progress edits alone.
    unchanged = callbacks["refresh_identity"](False, service.user)
    assert len(unchanged) == 18
    assert all(update == gr.update() for update in unchanged[1:])


def test_login_failure_clears_password_and_keeps_form_open(service, monkeypatch):
    def rejected(*args):
        raise StudyError("MOODLE_LOGIN_REQUIRED")
    monkeypatch.setattr(service, "login", rejected)
    ui = build_ui(service)
    login = next(event.fn for event in ui.fns.values() if event.fn and event.fn.__name__ == "login")
    states, message, form, username, password, changed = login("moodle", "student", "secret", {})
    assert not states["moodle"] and not changed
    assert form["visible"] and username == "student"
    assert password["value"] == "" and password["type"] == "password"
    assert "secret" not in message


def test_connection_checks_detect_expired_sessions(service, monkeypatch):
    monkeypatch.setattr(service, "verify_identity", lambda: None)
    service.hub.sis_user = service.user
    service.hub.sis.state = "connected"
    monkeypatch.setattr(service.hub, "sync_sis", lambda: {"timetable": {"error": None}})
    states, _ = check_connections(service)
    assert all(states.values())
    def expired():
        raise StudyError("MOODLE_LOGIN_REQUIRED")
    monkeypatch.setattr(service, "verify_identity", expired)
    monkeypatch.setattr(service.ai, "rpc", lambda method: {"ready": False})
    states, message = check_connections(service)
    assert not any(states.values())
    assert "需要登入" in message
