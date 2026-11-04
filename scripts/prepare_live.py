"""Prepare the current user's first course/assignment locally, without sending AI data."""

import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE = "http://127.0.0.1:8766"


def data(response):
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(payload["error"]["code"])
    return payload["data"]


def prepare(client, launch, base=BASE):
    state = {"course_id": None, "assignment_id": None, "asset_ids": [],
             "excerpt": "", "requirements": []}
    client.get(launch).raise_for_status()
    courses = data(client.post(base + "/api/connections/moodle/restore"))
    if not courses:
        return state
    course = courses[0]
    state["course_id"] = course["id"]
    assignments = data(client.post(base + "/api/sync/moodle", params={"course_id": course["id"]}))
    if not assignments:
        return state
    assignment = assignments[0]
    refs = assignment.get("asset_ids", [])
    state.update(assignment_id=assignment["id"], asset_ids=refs)
    if refs:
        parsed = data(client.post(base + f"/api/assignments/{assignment['id']}/download",
                                  json={"selected_asset_ids": refs, "approved": True}))
        state.update(excerpt=parsed["excerpt"], requirements=parsed["requirements"])
    return state


def main():
    launch = (ROOT / ".local/app/launch-url.txt").read_text().strip()
    with httpx.Client(trust_env=False, follow_redirects=True, timeout=120) as client:
        state = prepare(client, launch)
    # Save empty selections too, so a different user's earlier selection cannot linger.
    (ROOT / ".local/app/prepared-live.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    refs = state["asset_ids"]
    print(json.dumps({"prepared": state["assignment_id"] is not None,
                      "assets": len(refs), "characters": len(state["excerpt"]),
                      "requirements": len(state["requirements"]), "ai_data_sent": False}))


if __name__ == "__main__":
    main()
