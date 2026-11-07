"""Publication checks must inspect exactly what Git can upload."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "check_repository.py"


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.name=Test Fixture", "-c", "user.email=fixture@example.invalid",
         "-c", "commit.gpgSign=false", *args],
        cwd=repo, text=True, capture_output=True, check=True,
    ).stdout.strip()


def scan(repo, *args, updates=""):
    return subprocess.run(
        [sys.executable, str(repo / "scripts" / CHECKER.name), *args],
        cwd=repo, text=True, input=updates, capture_output=True, check=False,
    )


@pytest.fixture
def audit_repo(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(CHECKER, tmp_path / "scripts" / CHECKER.name)
    (tmp_path / "README.md").write_text("Synthetic repository.\n", encoding="utf-8")
    git(tmp_path, "add", "--", "README.md", "scripts")
    git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_scan_checks_staged_bytes_and_forced_private_files(audit_repo):
    sample = "synthetic-credential-for-index-test"
    source = audit_repo / "settings.json"
    source.write_text(json.dumps({"password": sample}) + "\n", encoding="utf-8")
    (audit_repo / ".gitignore").write_text("*.zip\n", encoding="utf-8")
    (audit_repo / "workspace.zip").write_bytes(b"synthetic artifact")
    git(audit_repo, "add", "--", "settings.json")
    git(audit_repo, "add", "-f", "--", "workspace.zip")
    source.write_text("{}\n", encoding="utf-8")

    result = scan(audit_repo)
    assert result.returncode == 1
    assert "index: 'settings.json': literal credential" in result.stderr
    assert "index: 'workspace.zip': private file" in result.stderr
    assert sample not in result.stderr


def test_pre_push_checks_a_secret_added_and_deleted_within_the_push(audit_repo):
    sample = "gh" + "p_" + "a" * 36
    base = git(audit_repo, "rev-parse", "HEAD")
    (audit_repo / "sample.html").write_text(sample, encoding="utf-8")
    git(audit_repo, "add", "--", "sample.html")
    git(audit_repo, "commit", "-qm", "add synthetic credential")
    git(audit_repo, "rm", "-q", "--", "sample.html")
    git(audit_repo, "commit", "-qm", "remove synthetic credential")
    tip = git(audit_repo, "rev-parse", "HEAD")
    assert scan(audit_repo).returncode == 0

    result = scan(audit_repo, "--pre-push",
                  updates=f"refs/heads/main {tip} refs/heads/main {base}\n")
    assert result.returncode == 1
    assert "'sample.html': GitHub token" in result.stderr
    assert sample not in result.stderr
