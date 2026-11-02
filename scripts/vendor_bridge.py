"""Copy an exact, license-preserving subset from the pinned upstream checkout."""

import hashlib
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
source = ROOT / ".upstream/ntpu-ai-vscode"
destination = ROOT / "ntpu-ai-bridge/vendor"
files = ["src/browser.js", "src/browser/manager.js", "src/browser/diagnostics.js",
         "src/browser/response-watchdog.js", "src/security/attachments.js", "src/security/guard.js",
         "src/security/offline-ocr.js", "src/security/ocr-worker.js", "LICENSE"]
manifest = {"source_commit": "f8101fb112b9b1364386becbcd8d922b68eec335", "files": {}}
for name in files:
    target = destination / name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source / name, target)
    manifest["files"][name] = hashlib.sha256(target.read_bytes()).hexdigest()
(destination / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
print("Copied exact licensed BrowserBridge subset.")
