"""Check tracked/stageable files for private artifacts and obvious credential assignments."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
files = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=ROOT, text=True).splitlines()
issues = []
for name in files:
    if any(part in name.split("/") for part in (".local", ".upstream", ".venv", "browser-profiles")) or name.endswith((".sqlite3", ".pdf", ".docx", ".ttf", ".log")):
        issues.append(name + ": private artifact must not be tracked")
    path = ROOT / name
    if path.is_file() and path.suffix in {".py", ".js", ".cjs", ".json", ".md", ".yml", ".toml"}:
        content = path.read_text(encoding="utf-8-sig")
        if re.search(r'(?:wstoken|password)\s*[=:]\s*[\"\'][A-Za-z0-9_-]{24,}[\"\']', content, re.I):
            issues.append(name + ": possible literal credential")
if issues:
    raise SystemExit("\n".join(issues))
print(f"Repository privacy check passed ({len(files)} files).")
