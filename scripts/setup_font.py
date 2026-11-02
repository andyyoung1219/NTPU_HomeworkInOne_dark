import hashlib
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

config = json.loads((ROOT / "docs/font.lock.json").read_text(encoding="utf-8-sig"))
target = ROOT / ".local/fonts/NotoSansTC-Variable.ttf"
target.parent.mkdir(parents=True, exist_ok=True)
if not target.exists():
    with urllib.request.urlopen(config["url"], timeout=60) as response:
        data = response.read(30 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != config["sha256"]:
        raise SystemExit("Font SHA-256 mismatch")
    target.write_bytes(data)
if hashlib.sha256(target.read_bytes()).hexdigest() != config["sha256"]:
    raise SystemExit("Font SHA-256 mismatch")
from backend.services.reports import prepare_font

prepare_font()
print("Noto Sans TC installed and hash verified.")
