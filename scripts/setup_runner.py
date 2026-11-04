"""Explicit opt-in build; runtime only uses the resulting immutable local image ID."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if __name__ == "__main__":
    from backend.agent.runner import DockerRunner, container_cli

    executable = container_cli()
    build_env = dict(os.environ)
    build_env["PATH"] = str(Path(executable).parent) + os.pathsep + build_env.get("PATH", "")
    contexts = subprocess.check_output([executable, "context", "ls", "--format", "{{.Name}}"], text=True).splitlines()
    context = "desktop-linux" if "desktop-linux" in contexts else None
    command = [executable, "--context", context] if context else [executable]
    try:
        version = subprocess.check_output([*command, "version", "--format", "{{json .Server}}"], text=True)
    except subprocess.CalledProcessError:
        raise SystemExit("Docker Desktop's Linux engine is not ready. Open Docker Desktop, resolve its "
                         "startup message, then rerun this script.") from None
    if "podman" in version.lower():
        raise SystemExit("The Docker CLI is connected to Podman. Start Docker Desktop and rerun this script; "
                         "the runner requires Docker's tmpfs inode limits.")
    print("Building with Docker context: " + (context or "current"), flush=True)
    subprocess.run([*command, "build", "--pull", "--network=none", "-t", "ntpu-p3-runner:local",
                    str(ROOT / "runner")], check=True, env=build_env)
    image = subprocess.check_output([*command, "image", "inspect", "ntpu-p3-runner:local",
                                    "--format", "{{.Id}}"], text=True).strip()
    if not image.startswith("sha256:"):
        image = "sha256:" + image
    target = ROOT / ".local/app/runner.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_suffix(".pending.json")
    try:
        pending.write_text(json.dumps({"image_id": image, "context": context}), encoding="utf-8")
        runner = DockerRunner(pending)
        result = runner.run("import os\nassert os.getuid() != 0\nprint('runner ready')\n", {},
                            target.parent / "runner-check", threading.Event(), 30)
        if result["exit_code"] != 0 or "runner ready" not in result["stdout"]:
            raise SystemExit("Runner execution check failed.")
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)
    print("Runner ready; immutable image ID saved to .local/app/runner.json")
