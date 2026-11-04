"""Opt-in REAL Docker gate. CI sets NTPU_TEST_RUNNER=1; unavailability then fails."""

import json
import os
import threading

import pytest
from test_completion import CompletionAI
from test_mvp import await_task
from test_mvp import service as service
from test_p3 import p3_request

from backend.agent.runner import DockerRunner
from backend.p0.evidence import ROOT
from backend.services.study import StudyError

pytestmark = pytest.mark.skipif(os.environ.get("NTPU_TEST_RUNNER") != "1",
                                reason="Real Docker gate requires NTPU_TEST_RUNNER=1 and built image")


@pytest.fixture
def runner():
    result = DockerRunner(ROOT / ".local/app/runner.json")
    assert result.status()["available"], "Build the Linux image with scripts/setup_runner.py first"
    return result


def test_real_sandbox_blocks_host_secrets_network_and_root_writes(runner, tmp_path, monkeypatch):
    secret = tmp_path / "host-private.txt"
    secret.write_text("SYNTHETIC_PRIVATE_SENTINEL", encoding="utf-8")
    monkeypatch.setenv("NTPU_TEST_PRIVATE_VALUE", "SYNTHETIC_PRIVATE_SENTINEL")
    code = f'''import os, socket
from pathlib import Path
assert os.getuid() != 0
assert not Path({str(secret)!r}).exists()
assert not Path("/var/run/docker.sock").exists()
assert "NTPU_TEST_PRIVATE_VALUE" not in os.environ
try:
    Path("/etc/ntpu-write-test").write_text("no")
except OSError:
    pass
else:
    raise AssertionError("writable root")
try:
    socket.create_connection(("1.1.1.1", 443), timeout=1)
except OSError:
    pass
else:
    raise AssertionError("network reachable")
Path("/output/checks.json").write_text('{{"isolation": true}}')
print("isolation passed")
'''
    result = runner.run(code, {}, tmp_path / "runs", threading.Event(), 30)
    assert result["exit_code"] == 0
    assert "isolation passed" in result["stdout"]
    assert result["parameters"]["network"] == "none"


def test_real_sandbox_limits_files_and_output(runner, tmp_path):
    code = '''from pathlib import Path
count = 0
try:
    for number in range(1000):
        Path(f"/output/f{number}.txt").write_text("x")
        count += 1
except OSError:
    pass
assert count < 1000
try:
    Path("/tmp/huge").write_bytes(b"x" * 2000000)
except OSError:
    pass
else:
    raise AssertionError("file size unlimited")
print("limits passed")
'''
    result = runner.run(code, {}, tmp_path / "runs", threading.Event(), 30)
    assert result["exit_code"] == 0 and "limits passed" in result["stdout"]


def test_real_sandbox_timeout_and_cancel(runner, tmp_path):
    with pytest.raises(StudyError, match="TOOL_TIMEOUT"):
        runner.run("while True: pass", {}, tmp_path / "timeout", threading.Event(), 2)
    cancel = threading.Event()
    timer = threading.Timer(1, cancel.set)
    timer.start()
    try:
        with pytest.raises(StudyError, match="CANCELLED"):
            runner.run("while True: pass", {}, tmp_path / "cancel", cancel, 30)
    finally:
        timer.cancel()


def test_direct_completion_uses_real_container_evidence_before_review(service, runner, monkeypatch):
    monkeypatch.setattr(service, "verify_identity", lambda: None)
    service.agent.runner = runner

    def report(payload, model):
        evidence = payload["execution"]
        assert evidence["exit_code"] == 0
        assert json.loads(evidence["stdout"]) == {"sum": 6}
        assert evidence["parameters"]["network"] == "none"
        assert evidence["image_id"] == runner.image()
        model["sections"][0]["content"] = "The executed program verified that 1 + 2 + 3 equals 6."
        return model

    service.ai = CompletionAI(execution=True, report_factory=report)
    task = service.complete_assignment("2", "real-direct-completion")
    result = await_task(service, task["id"], timeout=30)
    assert result["status"] == "waiting_review", result.get("error")
    assert result["completion"] == {"status": "complete", "blockers": [], "required_actions": []}
    assert len(result["runs"]) == 1 and len(service.ai.calls) == 3
    assert result["checkpoint"]["cursor"] == len(result["plan"]["steps"])
    assert "The executed program verified" in result["draft"]
    assert "TODO" not in result["draft"]
    source = result["generated_files"]["analysis.py"]
    assert service.safe_path(source["path"]).is_file()
    assert not result["reviewed"] and not result.get("pdf")


@pytest.mark.parametrize("mode", ["summary", "literature_comparison", "csv_analysis", "coding_test", "report_draft"])
@pytest.mark.parametrize("repetition", range(3))
def test_real_runner_task_gate(service, runner, mode, repetition):
    # AI remains synthetic; no university account, Cookie, or Token is used in CI.
    service.agent.runner = runner
    task = service.create_task(p3_request(service, mode), f"real-{mode}-{repetition}")
    service.approve(task["id"], task["request_sha256"], high=mode in {"csv_analysis", "coding_test"})
    result = await_task(service, task["id"], timeout=30)
    assert result["status"] == "waiting_review", json.dumps(result.get("tool_audit"))
    if mode == "csv_analysis":
        assert result["statistics_verified"]
