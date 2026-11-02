"""Print only local task status, never prompts, model text or credentials."""
import json
import sqlite3
from pathlib import Path

root = Path(__file__).resolve().parents[1]
connection = sqlite3.connect(root / ".local/app/study.sqlite3")
rows = connection.execute("SELECT payload FROM records WHERE kind='task'").fetchall()
values = [json.loads(row[0]) for row in rows]
print(json.dumps([{key: task.get(key) for key in ("id", "status", "error", "reviewed", "qa")}
                  for task in values], ensure_ascii=False))
