"""Runner commands use the engine context saved during setup."""

import io
import json
import threading

import pytest

from backend.agent import runner
from backend.services.study import StudyError

IMAGE = "sha256:" + "a" * 64
RUN = "b" * 32


@pytest.mark.parametrize("context", [None, "desktop-linux"])
def test_runner_keeps_saved_context_instead_of_global_default(tmp_path, monkeypatch, context):
    config = tmp_path / "runner.json"
    config.write_text(json.dumps({"image_id": "sha256:" + "a" * 64, "context": context}))
    monkeypatch.setattr(runner, "container_cli", lambda: "docker-test")
    sandbox = runner.DockerRunner(config)
    command = sandbox.engine_command()
    assert command == (["docker-test", "--context", context] if context else ["docker-test"])


def test_legacy_runner_configuration_still_uses_current_engine(tmp_path, monkeypatch):
    config = tmp_path / "runner.json"
    config.write_text(json.dumps({"image_id": "sha256:" + "a" * 64}))
    monkeypatch.setattr(runner, "container_cli", lambda: "docker-test")
    assert runner.DockerRunner(config).engine_command() == ["docker-test"]


def test_container_runner_mounts_only_the_current_input_subdirectory(tmp_path, monkeypatch):
    monkeypatch.setenv("NTPU_RUNNER_DATA_VOLUME", "ntpu-homework-data")
    monkeypatch.setattr(runner, "CONTAINER_DATA_ROOT", tmp_path)
    staging = tmp_path / "workspaces" / "account" / "task" / "work" / "runs" / RUN / "input"
    staging.mkdir(parents=True)
    command = runner.DockerRunner.command(IMAGE, "ntpu-test", staging)
    assert command.count("--mount") == 1
    assert command[command.index("--mount") + 1] == (
        "type=volume,source=ntpu-homework-data,"
        f"volume-subpath=workspaces/account/task/work/runs/{RUN}/input,"
        "volume-nocopy,target=/input,readonly"
    )
    assert "--network=none" in command and "--read-only" in command
    assert "--user=65532:65532" in command and "--cap-drop=ALL" in command
    assert str(tmp_path) not in " ".join(command)


@pytest.mark.parametrize("volume", ["", "../data", "bad,target=/data", "a", "bad\nvolume"])
def test_runner_rejects_invalid_configured_volume_names(tmp_path, monkeypatch, volume):
    monkeypatch.setenv("NTPU_RUNNER_DATA_VOLUME", volume)
    with pytest.raises(StudyError, match="SANDBOX_CONFIG_INVALID"):
        runner.DockerRunner.command(IMAGE, "ntpu-test", tmp_path / RUN / "input")


def test_runner_rejects_mounts_outside_run_input(tmp_path, monkeypatch):
    monkeypatch.setenv("NTPU_RUNNER_DATA_VOLUME", "ntpu-homework-data")
    data_root = tmp_path / "data"
    monkeypatch.setattr(runner, "CONTAINER_DATA_ROOT", data_root)
    for staging in (data_root, data_root / "input", tmp_path / RUN / "input",
                    data_root / "unsafe,target" / RUN / "input"):
        with pytest.raises(StudyError, match="SANDBOX_CONFIG_INVALID"):
            runner.DockerRunner.command(IMAGE, "ntpu-test", staging)


def test_container_runner_stages_files_before_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("NTPU_RUNNER_DATA_VOLUME", "ntpu-homework-data")
    monkeypatch.setattr(runner, "CONTAINER_DATA_ROOT", tmp_path)
    monkeypatch.setattr(runner, "container_cli", lambda: "docker-test")
    config = tmp_path / "runner.json"
    config.write_text(json.dumps({"image_id": IMAGE}), encoding="utf-8")
    filename = "a" * 64 + ".csv"
    program = "print('sample')"
    cleanup = []

    class Process:
        stdout = io.BytesIO(b'{"exit_code":0,"stdout":"sample","files":{}}')
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout):
            return 0

    def launch(command, **kwargs):
        mount = command[command.index("--mount") + 1]
        subpath = next(field.split("=", 1)[1] for field in mount.split(",")
                       if field.startswith("volume-subpath="))
        staged = runner.filesystem_path(tmp_path / subpath)
        assert staged.parent.parent == runner.filesystem_path(tmp_path / "runs")
        assert set(path.name for path in staged.iterdir()) == {filename, "program.py"}
        assert (staged / filename).read_bytes() == b"x,y\n1,2\n"
        assert (staged / "program.py").read_text(encoding="utf-8") == program
        return Process()

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    monkeypatch.setattr(runner.subprocess, "run", lambda command, **kwargs: cleanup.append(command))
    result = runner.DockerRunner(config).run(program, {filename: b"x,y\n1,2\n"},
                                              tmp_path / "runs", threading.Event(), 5)
    assert result["stdout"] == "sample"
    assert cleanup == [["docker-test", "rm", "-f", "ntpu-p3-" + result["run_id"]]]
