"""Allowlisted, content-free integration evidence. Never log exception messages."""

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local" / "p0"
_lock = threading.Lock()
FIELDS = {
    "passed", "code", "http_status", "course_count", "assignment_count", "asset_count",
    "sha256", "size_bytes", "format", "pages", "characters", "trial", "parseable",
    "document_matched", "cancelled", "ready", "source_sha", "authentication",
}


def record(event, **fields):
    if set(fields) - FIELDS:
        raise ValueError("Unapproved evidence field")
    entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
    LOCAL.mkdir(parents=True, exist_ok=True)
    with _lock, (LOCAL / "evidence.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def entries():
    path = LOCAL / "evidence.jsonl"
    if not path.exists():
        return []
    with _lock:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
