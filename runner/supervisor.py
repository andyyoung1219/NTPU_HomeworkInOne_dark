"""Trusted container entrypoint. No application credentials exist in this image."""

import base64
import json
import os
import re
import resource
import subprocess
import sys
from pathlib import Path

resource.setrlimit(resource.RLIMIT_FSIZE, (1048576, 1048576))
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
with open("/tmp/stdout", "wb") as out:
    result = subprocess.run([sys.executable, "-I", "-B", "/input/program.py"],
        stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"})
files = {}
for path in sorted(Path("/output").iterdir()):
    if len(files) >= 16:
        break
    if (path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}\.(json|txt|csv|png)", path.name)):
        continue
    # Refuse special files and race-swapped links from surviving child processes.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(1_000_001)
    if len(data) <= 1_000_000:
        files[path.name] = base64.b64encode(data).decode()
    if sum(len(value) for value in files.values()) > 1_500_000:
        raise SystemExit(2)
print(json.dumps({"exit_code": result.returncode,
    "stdout": Path("/tmp/stdout").read_bytes()[:16000].decode("utf-8", errors="replace"),
    "files": files, "packages": {"python": sys.version.split()[0], "dependencies": "stdlib"}}))
