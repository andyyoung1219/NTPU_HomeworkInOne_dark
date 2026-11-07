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


@pytest.mark.parametrize("legacy", [False, True], ids=["agent", "legacy"])
@pytest.mark.parametrize("language_source", ["requirements", "documents", None])
def test_report_language_policy_reaches_model_and_preserves_response(service, legacy, language_source):
    request = task_request(service)
    english_required = language_source is not None
    if language_source == "requirements":
        request["requirements"] = "Write the report in English."
    elif language_source == "documents":
        request["excerpt"] += "\nWrite the report in English."
    # The fixture document is already English. Without an explicit delivery
    # language, the prompt still needs to specify the Traditional Chinese default.
    content = "Please review the proposed method." if english_required else "請核對提出的方法。"

    def begin(prompt):
        service.ai.calls.append(prompt)
        future = concurrent.futures.Future()
        future.set_result(json.dumps({
            "title": "Method report" if english_required else "方法報告",
            "sections": [{"heading": "Method" if english_required else "方法", "content": content}],
            "sources": ["a" * 24], "unresolved": [],
        }, ensure_ascii=False))
        return "language-policy", future

    service.ai.begin = begin
    task = service.create_task(request, f"language-{legacy}-{language_source}")
    if legacy:
        task["plan"] = None
        service.store.save(service.user, "task", task["id"], task)
    service.approve(task["id"], task["request_sha256"])
    result = await_task(service, task["id"])
    assert result["status"] == "waiting_review", result.get("error")
    prompt = service.ai.calls[0]
    assert "未指定時使用繁體中文" in prompt
    assert "本次題目與需求清單明確指定的語言" in prompt
    assert "完整繁體中文報告" not in prompt
    assert "TODO。使用繁體中文。" not in prompt
    assert "JSON；使用繁體中文。" not in prompt
    assert "你是繁體中文作業助手" not in prompt
    if english_required:
        assert "Write the report in English." in prompt
    assert content in result["draft"]
    assert result["report_model"]["sections"][0]["content"] == content


def test_ui_has_two_centers_and_separate_workflow_screens(service):
    ui = build_ui(service)
    components = ui.config["components"]
    labels = [c["props"].get("label") for c in components]
    buttons = [c["props"].get("value") for c in components if c["type"] == "button"]
    assert "進入學習資料中心 →" in buttons
    assert "進入懶人作業中心 →" in buttons
    assert "重新產生草稿" in buttons
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
    assert len(unchanged) == 29
    assert all(update == gr.update() for update in unchanged[1:])


@pytest.mark.parametrize("case", ["assignment", "no_assignments", "no_courses", "other_user"])
def test_wizard_defaults_to_first_course_and_assignment_or_empty(service, case):
    # Insert alternate names before the expected first entries. Neither
    # label should override the order returned for the current account.
    service.store.save_many(service.user, [
        ("course", "later", {"id": "later", "name": "測試課程乙"}),
        ("course", "first", {"id": "first", "name": "資料結構"}),
        ("assignment", "later", {"id": "later", "course_id": "first", "title": "測試作業乙",
                                    "description": "另一份作業", "asset_ids": []}),
        ("assignment", "first", {"id": "first", "course_id": "first", "title": "排序練習",
                                    "description": "排序題目", "asset_ids": []}),
    ])
    if case == "no_assignments":
        service.store.save(service.user, "course", "empty", {"id": "empty", "name": "無作業課程"})
    if case in {"no_courses", "other_user"}:
        service.user = "c" * 24
    if case == "other_user":
        service.store.save_many(service.user, [
            ("course", "their-course", {"id": "their-course", "name": "線性代數"}),
            ("assignment", "their-assignment", {"id": "their-assignment", "course_id": "their-course",
                                                   "title": "矩陣練習", "description": "矩陣題目", "asset_ids": []}),
        ])
    ui = build_ui(service)
    restore = next(e.fn for e in ui.fns.values() if e.fn and e.fn.__name__ == "restore_workspace")
    values = restore()
    expected = {"assignment": ("first", "first"), "no_assignments": ("empty", None),
                "no_courses": (None, None), "other_user": ("their-course", "their-assignment")}
    assert (values[0]["value"], values[1]["value"]) == expected[case]


def test_step_one_selection_events_update_data_without_page_navigation(service):
    service.store.save_many(service.user, [
        ("course", "3", {"id": "3", "name": "第二門課"}),
        ("assignment", "4", {"id": "4", "course_id": "3", "title": "第二門課作業",
                              "description": "測試題目", "asset_ids": []}),
        ("assignment", "5", {"id": "5", "course_id": "3", "title": "另一份作業",
                              "description": "另一個題目", "asset_ids": []}),
    ])
    ui = build_ui(service)
    events = {e.fn.__name__: e for e in ui.fns.values() if e.fn}
    for name in ("switch_course", "select_assignment"):
        # Dropdown events and sync follow-ups cannot change either the current
        # page State or the visibility of any wizard screen, even on blur.
        assert not any(isinstance(component, (gr.State, gr.Column)) for component in events[name].outputs)
    switched = events["switch_course"].fn("3")
    assert len(switched) == len(events["switch_course"].outputs)
    assert switched[0]["value"] == "3"
    assert set(value for label, value in switched[1]["choices"]) == {"4", "5"}
    selected = events["select_assignment"].fn("3", "4")
    assert len(selected) == len(events["select_assignment"].outputs)
    assert selected[1]["value"] == "4"
    assert "第二門課作業" in selected[2] and selected[3] == "測試題目"
    assert all(value == gr.update() for value in events["select_assignment"].fn("3", "2"))
    # Clearing a selector stays empty instead of restoring an implicit default.
    cleared = events["select_assignment"].fn("3", None)
    assert cleared[1]["value"] is None and cleared[2] == "" and cleared[5] == ""
    cleared_course = events["switch_course"].fn(None)
    assert cleared_course[0]["value"] is None and cleared_course[1]["choices"] == []
    # Home-page shortcuts are explicit navigation and still open step two.
    assert events["open_from_dashboard"].fn("4")[-8] == "sources"


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


def test_regenerate_resends_current_inputs_and_preserves_previous_draft(service):
    request = {**task_request(service), "data_transfer_approved": True}
    original = service.create_task(request, "original-draft")
    original = await_task(service, original["id"])
    ui = build_ui(service)
    regenerate = next(e.fn for e in ui.fns.values() if e.fn and e.fn.__name__ == "regenerate")
    outputs = list(regenerate(original["id"], request["assignment_id"], request["selected_asset_ids"],
                              request["mode"], "請重新整理並補充說明", request["excerpt"], "核對來源"))
    new_id = next(row[0] for row in outputs if isinstance(row[0], str) and row[0])
    assert new_id != original["id"]
    result = service.get("task", new_id)
    assert result["status"] == "waiting_review"
    assert result["request"]["instruction"] == "請重新整理並補充說明"
    assert result["request"]["requirements"] == "核對來源"
    assert result["request"]["selected_asset_ids"] == request["selected_asset_ids"]
    assert len(service.ai.calls) == 2
    assert service.get("task", original["id"]) == original
    assert not result["reviewed"]


def test_regenerate_rejects_running_or_mismatched_task_without_resending(service):
    request = task_request(service)
    task = service.create_task(request, "pending-regenerate")
    ui = build_ui(service)
    regenerate = next(e.fn for e in ui.fns.values() if e.fn and e.fn.__name__ == "regenerate")
    args = [task["id"], "2", request["selected_asset_ids"], request["mode"], "", request["excerpt"], ""]
    task["status"] = "running"
    service.store.save(service.user, "task", task["id"], task)
    with pytest.raises(gr.Error, match="仍在執行"):
        list(regenerate(*args))
    task["status"] = "failed"
    service.store.save(service.user, "task", task["id"], task)
    args[1] = "different-assignment"
    with pytest.raises(gr.Error, match="作業已變更"):
        list(regenerate(*args))
    assert len(service.store.list(service.user, "task")) == 1
    assert service.ai.calls == []
