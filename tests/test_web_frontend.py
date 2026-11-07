"""The production UI is static, authenticated and independent of Gradio."""

import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient
from test_mvp import service as service
from test_mvp import task_request

from backend.main import ORIGIN, create_app


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
