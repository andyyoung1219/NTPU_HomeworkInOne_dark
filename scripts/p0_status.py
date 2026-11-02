"""Print only allowlisted live evidence; never read browser profiles or credentials."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.p0.evidence import entries
from backend.p0.gate import evaluate

print(json.dumps(evaluate(entries()), ensure_ascii=False, indent=2))
