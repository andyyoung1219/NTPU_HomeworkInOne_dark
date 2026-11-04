"""Runner commands use the engine context saved during setup."""

import json

import pytest

from backend.agent import runner


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
