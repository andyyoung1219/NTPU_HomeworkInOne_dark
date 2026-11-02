"""Prepare the explicitly selected P1 assignment locally, without any AI transmission."""

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

launch = (ROOT / ".local/app/launch-url.txt").read_text().strip()
base = "http://127.0.0.1:8766"


def data(response):
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"]["code"])
    return payload["data"]


with httpx.Client(trust_env=False, follow_redirects=True, timeout=120) as client:
    client.get(launch).raise_for_status()
    courses = data(client.post(base + "/api/connections/moodle/restore"))
    matches = [c for c in courses if "人工智慧代理人原理及實務" in c["name"]]
    if len(matches) != 1:
        raise RuntimeError("TARGET_COURSE_NOT_UNIQUE")
    course = matches[0]
    assignments = data(client.post(base + "/api/sync/moodle", params={"course_id": course["id"]}))
    matches = [a for a in assignments if "指定作業1" in a["title"].replace(" ", "")]
    if len(matches) != 1:
        raise RuntimeError("TARGET_ASSIGNMENT_NOT_UNIQUE")
    assignment = matches[0]
    refs = assignment["asset_ids"]
    if not refs:
        raise RuntimeError("NO_SUPPORTED_ATTACHMENTS")
    parsed = data(client.post(base + f"/api/assignments/{assignment['id']}/download",
                              json={"selected_asset_ids": refs, "approved": True}))
    state = {"course_id": course["id"], "assignment_id": assignment["id"], "asset_ids": refs,
             "excerpt": parsed["excerpt"], "requirements": parsed["requirements"]}
    (ROOT / ".local/app/prepared-live.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"prepared": True, "assets": len(refs), "characters": len(parsed["excerpt"]),
                      "requirements": len(parsed["requirements"]), "ai_data_sent": False}))
