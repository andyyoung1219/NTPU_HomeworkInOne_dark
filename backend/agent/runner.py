"""Docker-only runner; never evaluates submitted Python on the host."""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from backend.agent.contracts import fail

MAX_OUTPUT = 2 * 1024 * 1024
CREATE_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CONTAINER_DATA_ROOT = Path("/data")


def container_cli():
    executable = shutil.which("docker")
    if executable:
        return executable
    if os.name == "nt":
        for base, suffix in (("LOCALAPPDATA", "Programs/DockerDesktop/resources/bin/docker.exe"),
                             ("ProgramFiles", "Docker/Docker/resources/bin/docker.exe")):
            candidate = Path(os.environ.get(base, "")) / suffix
            if candidate.is_file():
                return str(candidate)
    fail("SANDBOX_NOT_AVAILABLE")


def filesystem_path(path):
    # Keep Docker mount arguments normal, but use extended paths for Windows IO
    # when nested task/run directories plus SHA256 filenames exceed MAX_PATH.
    absolute = str(Path(path).resolve())
    if os.name == "nt" and not absolute.startswith("\\\\?\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:] if absolute.startswith("\\\\") else "\\\\?\\" + absolute)
    return Path(absolute)


def runner_data_volume():
    volume = os.environ.get("NTPU_RUNNER_DATA_VOLUME")
    if volume is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]+", volume):
        fail("SANDBOX_CONFIG_INVALID")
    return volume


class DockerRunner:
    def __init__(self, config):
        self.config = Path(config)

    def image(self):
        if not self.config.is_file():
            fail("SANDBOX_NOT_AVAILABLE")
        try:
            image = json.loads(self.config.read_text(encoding="utf-8"))["image_id"]
        except (ValueError, KeyError):
            fail("SANDBOX_CONFIG_INVALID")
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", image):
            fail("SANDBOX_IMAGE_NOT_PINNED")
        return image

    def engine_command(self):
        config = json.loads(self.config.read_text(encoding="utf-8"))
        command = [container_cli()]
        if config.get("context"):
            command.extend(["--context", config["context"]])
        return command

    def status(self):
        try:
            runner_data_volume()
            image = self.image()
            info = subprocess.run([*self.engine_command(), "image", "inspect", image, "--format", "{{.Os}}"],
                capture_output=True, timeout=5, creationflags=CREATE_FLAGS)
            return {"available": info.returncode == 0 and info.stdout.strip() == b"linux",
                    "image_id": image, "network": "none"}
        except Exception:
            return {"available": False, "code": "SANDBOX_NOT_AVAILABLE", "network": "none"}

    @staticmethod
    def command(image, name, staging):
        resolved = staging.resolve()
        mount = f"type=bind,source={resolved},target=/input,readonly"
        if (volume := runner_data_volume()) is not None:
            data_root = CONTAINER_DATA_ROOT
            if resolved != staging.absolute() or not resolved.is_relative_to(data_root):
                fail("SANDBOX_CONFIG_INVALID")
            subpath = resolved.relative_to(data_root).as_posix()
            if not re.fullmatch(r"(?:[A-Za-z0-9_.-]+/)*[a-f0-9]{32}/input", subpath):
                fail("SANDBOX_CONFIG_INVALID")
            mount = f"type=volume,source={volume},volume-subpath={subpath},volume-nocopy,target=/input,readonly"
        # No host output mount, Docker socket, environment forwarding, or shell.
        return ["docker", "run", "--rm", "--pull=never", "--name", name,
                "--network=none", "--read-only", "--user=65532:65532",
                "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
                "--pids-limit=32", "--memory=256m", "--memory-swap=256m", "--cpus=1",
                "--ulimit=nofile=64:64", "--ulimit=fsize=1048576:1048576",
                "--ulimit=core=0:0", "--log-driver=none",
                "--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=16m,nr_inodes=128,mode=1777",
                "--tmpfs=/output:rw,noexec,nosuid,nodev,size=8m,nr_inodes=64,mode=1777",
                "--mount", mount,
                "--workdir=/output", "--entrypoint=python", image,
                "-I", "-B", "/opt/runner.py"]

    def run(self, code, inputs, directory, cancel, timeout):
        image = self.image()
        run_id = uuid.uuid4().hex
        directory = Path(directory) / run_id
        staging = directory / "input"
        container_name = "ntpu-p3-" + run_id
        filesystem_path(staging).mkdir(parents=True)
        command = self.command(image, container_name, staging)
        hashes = {}
        for name, data in inputs.items():
            if not re.fullmatch(r"[a-f0-9]{64}\.[a-z0-9]{1,8}", name) or len(data) > 10_000_000:
                fail("RUNNER_INPUT_INVALID")
            filesystem_path(staging / name).write_bytes(data)
            hashes[name] = hashlib.sha256(data).hexdigest()
        filesystem_path(staging / "program.py").write_text(code, encoding="utf-8")
        # Linux CI / rootless Docker must be able to read copies as the sandbox user.
        for path in [filesystem_path(directory), filesystem_path(staging), *filesystem_path(staging).iterdir()]:
            os.chmod(path, 0o755 if path.is_dir() else 0o644)
        process = None
        started = time.monotonic()
        chunks, size, overflow = [], [0], threading.Event()
        try:
            if cancel.is_set():
                fail("CANCELLED")
            command = self.engine_command() + command[1:]
            process = subprocess.Popen(command, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, creationflags=CREATE_FLAGS)

            def collect():
                while chunk := process.stdout.read(65536):
                    size[0] += len(chunk)
                    if size[0] > MAX_OUTPUT:
                        overflow.set()
                        return
                    chunks.append(chunk)

            reader = threading.Thread(target=collect, daemon=True)
            reader.start()
            while process.poll() is None:
                if cancel.wait(0.05):
                    fail("CANCELLED")
                if time.monotonic() - started > timeout:
                    fail("TOOL_TIMEOUT")
                if overflow.is_set():
                    fail("RUNNER_OUTPUT_LIMIT")
            reader.join(timeout=2)
            if overflow.is_set():
                fail("RUNNER_OUTPUT_LIMIT")
            if process.returncode:
                fail("SANDBOX_EXECUTION_FAILED")
            try:
                payload = json.loads(b"".join(chunks))
                if type(payload["exit_code"]) is not int or not isinstance(payload["stdout"], str):
                    raise ValueError()
                outputs = payload["files"]
                if not isinstance(outputs, dict) or len(outputs) > 16:
                    raise ValueError()
                artifacts = {}
                for filename, content in outputs.items():
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.(json|txt|csv|png)", filename):
                        raise ValueError()
                    data = base64.b64decode(content, validate=True)
                    if len(data) > 1_000_000:
                        raise ValueError()
                    target = directory / filename
                    filesystem_path(target).write_bytes(data)
                    artifacts[filename] = {"sha256": hashlib.sha256(data).hexdigest(),
                                           "bytes": len(data), "path": str(target)}
            except (ValueError, TypeError, KeyError):
                fail("RUNNER_RESULT_INVALID")
            result = {"run_id": run_id, "image_id": image, "source_hashes": hashes,
                      "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                      "parameters": {"timeout": timeout, "network": "none", "memory_mb": 256,
                                     "cpus": 1, "max_files": 64, "output_mb": 8},
                      "packages": payload.get("packages", {}), "exit_code": payload["exit_code"],
                      "stdout": payload["stdout"][:16000], "artifacts": artifacts}
            receipt = directory / "run.json"
            filesystem_path(receipt).write_text(json.dumps(result, indent=2), encoding="utf-8")
            artifacts["run.json"] = {"sha256": hashlib.sha256(filesystem_path(receipt).read_bytes()).hexdigest(),
                                     "bytes": filesystem_path(receipt).stat().st_size, "path": str(receipt)}
            return result
        finally:
            if process:
                try:
                    subprocess.run([*self.engine_command(), "rm", "-f", container_name], timeout=10,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_FLAGS)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
                process.stdout.close()
