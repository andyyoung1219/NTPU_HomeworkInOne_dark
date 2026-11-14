"""The production UI is static, authenticated and independent of Gradio."""

import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from test_mvp import service as service
from test_mvp import task_request

from backend.main import ACCOUNT_HEADER, ORIGIN, create_app


def test_backend_import_does_not_load_gradio():
    subprocess.run([sys.executable, "-c", "import backend.main, sys; "
                    "assert not any(n == 'gradio' or n.startswith('gradio.') for n in sys.modules)"],
                   check=True)


def test_authenticated_static_app_and_asset_boundaries(service):
    client = TestClient(create_app(service, "web-test"), base_url=ORIGIN)
    for path in ("/", "/static/app.js", "/static/styles.css"):
        assert client.get(path).status_code == 401
    response = client.get("/unlock/web-test", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    assert "HttpOnly" in response.headers["set-cookie"]
    home = client.get("/")
    assert home.status_code == 200
    assert 'lang="zh-Hant"' in home.text and '/static/app.js' in home.text
    assert "gradio" not in home.text.lower()
    assert "script-src 'self'" in home.headers["content-security-policy"]
    for name in ("app.js", "workflow.js", "ui.js", "styles.css", "tokens.css"):
        result = client.get("/static/" + name)
        assert result.status_code == 200
        assert result.headers["cache-control"] == "no-store"
    for path in ("/static/../gradio_app.py", "/static/%2e%2e/gradio_app.py",
                 "/static/active-user.json", "/config", "/gradio_api/info"):
        assert client.get(path).status_code == 404
    assert client.get("/static/app.js", headers={"Origin": "https://other.example"}).status_code == 403
    assert client.get("/", headers={"Host": "other.example"}).status_code == 403


def test_hub_request_keeps_account_and_rejects_response_after_switch(service, monkeypatch):
    client = TestClient(create_app(service, "account-test"), base_url=ORIGIN)
    client.get("/unlock/account-test", follow_redirects=False)
    old_user, new_user = service.user, "c" * 24
    old_scope = client.get("/api/courses").headers[ACCOUNT_HEADER]
    timetable_started, resume = threading.Event(), threading.Event()
    observed_users = []
    get = service.store.get

    def read(user, kind, identifier):
        if kind == "hub" and identifier.startswith("sis:"):
            observed_users.append(user)
            if identifier == "sis:timetable" and not timetable_started.is_set():
                timetable_started.set()
                assert resume.wait(5)
        return get(user, kind, identifier)

    monkeypatch.setattr(service.store, "get", read)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.get, "/api/hub", headers={ACCOUNT_HEADER: old_scope})
        try:
            assert timetable_started.wait(5)
            service.user = new_user
        finally:
            resume.set()
        response = request.result(timeout=5)

    assert observed_users == [old_user] * 4
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ACCOUNT_CHANGED_RECONNECT"
    assert response.json()["data"] is None
    assert client.get("/api/hub", headers={ACCOUNT_HEADER: old_scope}).status_code == 409
    assert client.get("/api/maintenance/diagnostics", params={"account_scope": old_scope}).status_code == 409
    assert client.get("/api/courses").headers[ACCOUNT_HEADER] != old_scope


def test_account_change_can_finish_its_own_request_but_old_tab_cannot_save(service, monkeypatch):
    client = TestClient(create_app(service, "account-test"), base_url=ORIGIN)
    client.get("/unlock/account-test", follow_redirects=False)
    old_scope = client.get("/api/courses").headers[ACCOUNT_HEADER]
    user = "c" * 24

    def adopt():
        service.activate_user(user)
        return user

    monkeypatch.setattr(service, "adopt_vault", adopt)
    monkeypatch.setattr(service, "sync_courses", lambda: [{"id": service.require_user()}])
    result = client.post("/api/connections/moodle/restore", headers={ACCOUNT_HEADER: old_scope})
    assert result.status_code == 200
    assert result.json()["data"] == [{"id": user}]
    assert result.headers[ACCOUNT_HEADER] != old_scope
    stale_save = client.put("/api/workflow/progress", json={}, headers={ACCOUNT_HEADER: old_scope})
    assert stale_save.status_code == 409
    assert service.store.get(user, "preferences", "workflow") is None


@pytest.mark.parametrize("endpoint", ["start", "restore"])
def test_ai_connection_actions_respect_running_task_lock(service, endpoint):
    client = TestClient(create_app(service, "web-test"), base_url=ORIGIN)
    client.get("/unlock/web-test")
    with service.ai_lock:
        response = client.post("/api/connections/ntpu-ai/" + endpoint)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AI_BUSY"
    response = client.post("/api/connections/ntpu-ai/" + endpoint)
    assert response.status_code == 200
    assert response.json()["data"]["ready"]


def test_task_status_reports_when_a_checkpoint_can_no_longer_resume(service):
    client = TestClient(create_app(service, "web-test"), base_url=ORIGIN)
    client.get("/unlock/web-test")
    task = service.create_task(task_request(service), "checkpoint-ui")
    task.update(status="partial", error="AI_TIMEOUT", deadline_at=time.time() + 60)
    path = f"/api/tasks/{task['id']}"
    service.store.save(service.user, "task", task["id"], task)
    assert client.get(path).json()["data"]["resume_unavailable"] is None

    step = task["plan"]["steps"][task["checkpoint"]["cursor"]]
    task["checkpoint"]["attempts"][step["id"]] = 1 + task["plan"]["limits"]["max_retries"]
    service.store.save(service.user, "task", task["id"], task)
    result = client.get(path).json()["data"]
    assert result["resume_unavailable"] == "RETRY_BUDGET_EXCEEDED"
    assert result["checkpoint"] == task["checkpoint"]
    assert result["error"] == "AI_TIMEOUT"
    assert not service.ai.calls
