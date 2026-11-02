"""Render the newest reviewed PDF for visual QA without exposing draft text."""
import json
import sqlite3
import sys
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.services.reports import validate_pdf

db = sqlite3.connect(ROOT / ".local/app/study.sqlite3")
tasks = [(row[0], json.loads(row[1])) for row in db.execute(
    "SELECT user_id,payload FROM records WHERE kind='task' ORDER BY updated_at DESC")]
user, task = next((user, task) for user, task in tasks if task["status"] == "completed")
path = ROOT / ".local/app" / task["pdf"]
output = ROOT / ".local/qa/pdf"
output.mkdir(parents=True, exist_ok=True)
with pymupdf.open(path) as document:
    for index, page in enumerate(document):
        page.get_pixmap(matrix=pymupdf.Matrix(1.25, 1.25)).save(output / f"page-{index + 1}.png")
result = {"task_id": task["id"], "path": str(path), "reviewed": task["reviewed"],
          "quality": validate_pdf(path), "source_hashes": task["source_hashes"]}
(ROOT / ".local/qa/live-pdf.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({"rendered": True, "pages": result["quality"]["pages"], "quality_passed": result["quality"]["passed"]}))
