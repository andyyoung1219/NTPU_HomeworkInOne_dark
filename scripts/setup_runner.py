"""Explicit opt-in build; runtime only uses the resulting immutable local image ID."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    subprocess.run(["docker", "build", "--pull", "-t", "ntpu-p3-runner:local", str(ROOT / "runner")], check=True)
    image = subprocess.check_output(["docker", "image", "inspect", "ntpu-p3-runner:local",
                                    "--format", "{{.Id}}"], text=True).strip()
    target = ROOT / ".local/app/runner.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"image_id": image}), encoding="utf-8")
    print("Runner ready; immutable image ID saved to .local/app/runner.json")
