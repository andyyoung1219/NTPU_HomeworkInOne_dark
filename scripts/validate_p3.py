"""Record repeatable P3 outcomes without school data or claiming live-AI validation."""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if __name__ == "__main__":
    output = ROOT / ".local/p3"
    output.mkdir(parents=True, exist_ok=True)
    real = os.environ.get("NTPU_TEST_RUNNER") == "1"
    target = "tests/test_p3_sandbox.py" if real else "tests/test_p3.py"
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    xml = output / f"gate-{stamp}.xml"
    result = subprocess.run([sys.executable, "-m", "pytest", target, "-q", "--tb=short",
                             "--basetemp", str(output / f"tmp-{stamp}"), f"--junitxml={xml}"], cwd=ROOT)
    cases = []
    if xml.exists():
        for case in ET.parse(xml).iter("testcase"):
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            problem = failure if failure is not None else error
            cases.append({"name": case.attrib["name"],
                "status": "failed" if problem is not None else "skipped" if skipped is not None else "passed",
                "reason": problem.attrib.get("message", "")[:1000] if problem is not None else
                          skipped.attrib.get("message", "") if skipped is not None else ""})
    report = {"at": datetime.now(UTC).isoformat(), "runner": "real Docker" if real else "synthetic",
              "ai": "synthetic", "live_school_gate": "not_run", "cases": cases,
              "passed": sum(c["status"] == "passed" for c in cases), "total": len(cases),
              "success_rate": sum(c["status"] == "passed" for c in cases) / len(cases) if cases else 0}
    (output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(result.returncode)
