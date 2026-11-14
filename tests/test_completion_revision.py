import base64
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from test_completion import automatic as automatic
from test_completion_deliverables import (
    MEASUREMENT_CODE,
    RecordedRunner,
    StructuredAI,
    deliverable,
)
from test_mvp import await_task
from test_mvp import service as service

from backend.agent.completion_units import merge_draft_edits
from backend.agent.contracts import digest
from backend.main import ORIGIN, create_app
from backend.p0.ai import AIError
from backend.services.exports import export_extra
from backend.services.study import StudyError


def upload(name, data):
    return {"name": name, "data": base64.b64encode(data).decode("ascii")}


def section(content, sources=None):
    return {"content": content, "sources": sources or [], "required_actions": []}


def test_revision_uses_manual_evidence_and_preserves_independent_run(automatic, monkeypatch):
    service = automatic
    service.agent.runner = runner = RecordedRunner()
    service.ai = ai = StructuredAI([
        deliverable("experiment", "External experiment", "python",
                    required_capabilities=["numpy"], expected_artifacts=["observations.json"]),
        deliverable("analysis", "Analyze observations", "writing", dependencies=["experiment"]),
        deliverable("discussion", "Discuss findings", "writing", optional_dependencies=["analysis"]),
        deliverable("baseline", "Independent baseline", "python", expected_artifacts=["measurement.json"]),
    ], {"baseline": {"code": MEASUREMENT_CODE}, "discussion": section("Previous discussion.")})
    parent = await_task(service, service.complete_assignment("2", "original")["id"])
    assert parent["status"] == "waiting_review"
    assert parent["tool_results"]["unit-baseline"]["status"] == "verified"
    service.save_draft(parent["id"], parent["draft"] + "\n\nManual review note.", reviewed=True,
                       accept_incomplete=True)
    parent = service.get("task", parent["id"])
    ai.calls.clear()
    ai.responses.update(analysis=section("Analysis of user-supplied observations."),
                        discussion=section("Updated discussion."))
    monkeypatch.setattr(service, "download", lambda *args: pytest.fail("Should reuse original snapshots"))

    body = {"unit_id": "experiment", "mode": "result", "notes": "Measured independently.",
            "files": [upload("observations.json", b'{"sum":9}')], "approved": True}
    client = TestClient(create_app(service, "revision-key"), base_url=ORIGIN)
    client.get("/unlock/revision-key", follow_redirects=False)
    path = f"/api/tasks/{parent['id']}/revise"
    headers = {"Idempotency-Key": "retry-same"}
    invalid = client.post(path, json={**body, "files": []}, headers=headers)
    assert invalid.json()["error"]["code"] == "REVISION_RESULTS_MISSING"
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 202, response.json()
    result = await_task(service, response.json()["data"]["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert not result["reviewed"]
    assert [step["id"] for step in result["plan"]["steps"][:-1]] == [
        "unit-experiment", "unit-analysis", "unit-discussion"]
    assert [call["deliverable"]["id"] for call in ai.calls] == ["analysis", "discussion"]
    evidence = ai.calls[0]["dependencies"][0]
    assert evidence["status"] == "user_supplied" and evidence["user_supplied_notes"] == body["notes"]
    assert evidence["files"][0]["excerpt"] == '{"sum":9}'
    assert result["tool_results"]["unit-experiment"]["status"] == "user_supplied"
    assert "Results supplied by the user" in result["draft"]
    assert "Updated discussion." in result["draft"]
    assert "Manual review note." in result["draft"]
    previous = parent["tool_results"]["unit-baseline"]
    baseline = result["tool_results"]["unit-baseline"]
    assert baseline["run_id"] == previous["run_id"] and len(runner.calls) == 1
    audit = next(item for item in result["tool_audit"] if item["step_id"] == "unit-baseline")
    assert audit["reused_from_task_id"] == parent["id"] and audit["result_sha256"] == digest(baseline)
    name = next(iter(baseline["artifacts"]))
    assert client.get(f"/api/tasks/{result['id']}/artifacts/{name}").content == b'{"sum":6}'
    supplied_name = next(iter(result["tool_results"]["unit-experiment"]["artifacts"]))
    assert client.get(f"/api/tasks/{result['id']}/artifacts/{supplied_name}").content == b'{"sum":9}'
    assert service.get("task", parent["id"]) == parent
    assert client.post(path, json=body, headers=headers).json()["data"]["id"] == result["id"]
    assert client.post(path, json={**body, "notes": "Different."}, headers=headers).status_code == 409
    service.save_draft(result["id"], result["draft"], reviewed=True, accept_incomplete=True)
    exported = export_extra(service, result["id"], "zip")
    with zipfile.ZipFile(service.safe_path(exported["exports"]["zip"]["path"])) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["revision"]["parent_task_id"] == parent["id"]
        assert archive.read(f"artifacts/unit-experiment/{supplied_name}") == b'{"sum":9}'


def test_supplemental_csv_is_scoped_to_selected_stage(automatic):
    service = automatic
    service.ai = ai = StructuredAI([
        deliverable("profile", "Profile readings", "writing", required_capabilities=["csv-profiling"]),
        deliverable("analysis", "Analyze readings", "writing", dependencies=["profile"]),
        deliverable("intro", "Describe the assignment", "writing"),
    ], {"intro": section("Independent introduction.")})
    parent = await_task(service, service.complete_assignment("2", "csv-original")["id"])
    assert parent["status"] == "waiting_review"
    ai.calls.clear()
    ai.responses.update(profile=section("New sample profile."), analysis=section("New interpretation."))
    body = {"unit_id": "profile", "mode": "supplement", "notes": "Use the new readings.",
            "files": [upload("新資料.CSV", b"x,y\n1,4\n2,6\n")], "approved": True}
    result = await_task(service, service.revise_completion(parent["id"], body, "csv-revision")["id"])
    assert result["status"] == "waiting_review", result.get("error")
    assert [call["deliverable"]["id"] for call in ai.calls] == ["profile", "analysis"]
    profile = ai.calls[0]
    artifact = profile["user_supplements"][0]["files"][0]
    assert artifact["name"] == "新資料.CSV" and artifact["sandbox_path"].endswith(".csv")
    assert profile["data"]["attachment_profiles"][0]["profile"]["records"] == 2
    assert "user_supplements" not in ai.calls[1]
    assert result["tool_results"]["unit-intro"] == parent["tool_results"]["unit-intro"]
    assert "New interpretation." in result["draft"]
    with pytest.raises(StudyError, match="REVISION_APPROVAL_REQUIRED"):
        service.revise_completion(parent["id"], {**body, "approved": False}, "no-approval")


def test_revision_resumes_from_its_own_source_snapshot(automatic):
    service = automatic
    service.ai = ai = StructuredAI([
        deliverable("notes", "Summarize the notes", "writing"),
        deliverable("report", "Explain the observations", "writing", dependencies=["notes"]),
        deliverable("intro", "Introduce the assignment", "writing"),
    ], {"notes": section("Original notes."), "report": section("Original analysis."),
        "intro": section("Independent introduction.")})
    parent = await_task(service, service.complete_assignment("2", "resume-original")["id"])
    edited = parent["draft"].replace("Independent introduction.", "Corrected introduction.")
    edited = edited.replace("Original analysis.", "Manual analysis before the rerun.")
    service.save_draft(parent["id"], edited)
    ai.responses["report"] = AIError("AI_AUTH_REQUIRED")
    body = {"unit_id": "notes", "mode": "result", "notes": "Corrected notes.", "files": [], "approved": True}
    blocked = await_task(service, service.revise_completion(parent["id"], body, "resume-revision")["id"])
    assert blocked["status"] == "blocked_auth"
    assert blocked["checkpoint"]["cursor"] == 1
    assignment = service.get("assignment", "2")
    assignment["description"] = "Moodle changed after the snapshot."
    service.store.save(service.user, "assignment", assignment["id"], assignment)
    ai.responses["report"] = section("Corrected analysis.")
    service.agent.resume(blocked["id"], blocked["request_sha256"], high=True)
    result = await_task(service, blocked["id"])
    assert result["status"] == "waiting_review"
    assert result["checkpoint"]["attempts"]["unit-notes"] == 1
    assert "Corrected analysis." in result["draft"]
    assert "Manual analysis before the rerun." not in result["draft"]
    assert "Corrected introduction." in result["draft"]
    assert "Independent introduction." not in result["draft"]
    assert result["revision"]["draft_edits_conflicted"]
    assert all("Corrected introduction." not in json.dumps(call) for call in ai.calls)

    ai.responses["report"] = section("Second regenerated analysis.")
    body = {"unit_id": "report", "mode": "retry", "notes": "", "files": [], "approved": True}
    second = await_task(service, service.revise_completion(result["id"], body, "second-revision")["id"])
    assert second["status"] == "waiting_review"
    assert "Corrected introduction." in second["draft"]
    assert "Second regenerated analysis." in second["draft"]
    assert not second["revision"]["draft_edits_conflicted"]
    assert service.get("task", parent["id"])["draft"] == edited


def test_merge_draft_edits_handles_insertions_and_deletions_around_updated_text():
    original = ("# Report\n\n## Stable\n\nKeep this.\nRemove this.\n\n"
                "## Updated\n\nEarlier result.\n\n## References\n\n- Fixture source\n")
    edited = original.replace("## Stable", "Opening note.\n\n## Stable")
    edited = edited.replace("Remove this.\n", "").replace("Earlier result.", "Conflicting edit.")
    edited += "\nClosing note.\n"
    regenerated = original.replace("Earlier result.", "Regenerated result.")
    draft, conflicted = merge_draft_edits(original, edited, regenerated)
    assert conflicted
    assert draft == (
        "# Report\n\nOpening note.\n\n## Stable\n\nKeep this.\n\n"
        "## Updated\n\nRegenerated result.\n\n## References\n\n- Fixture source\n\nClosing note.\n")


def test_merge_draft_edits_keeps_insertions_with_their_section():
    original = "# Report\n\n## First\n\nInitial result.\n\n## Third\n\nAnother result.\n"
    edited = original.replace("## Third", "Comment on the initial result.\n\n## Third")
    regenerated = original.replace("## Third", "## Second\n\nRecovered result.\n\n## Third")
    draft, conflicted = merge_draft_edits(original, edited, regenerated)
    assert not conflicted
    assert draft.index("Comment on the initial result.") < draft.index("## Second")

    regenerated = original.replace("## First\n\nInitial result.\n\n", "")
    draft, conflicted = merge_draft_edits(original, edited, regenerated)
    assert conflicted
    assert draft == regenerated


def test_retry_review_stage_without_new_input_reuses_saved_supplement_and_runs(automatic):
    service = automatic
    service.agent.runner = runner = RecordedRunner()
    service.ai = ai = StructuredAI([
        deliverable("baseline", "Compute the baseline", "python", expected_artifacts=["measurement.json"]),
        deliverable("profile", "Profile supplemental data", "writing", dependencies=["baseline"],
                    required_capabilities=["csv-profiling"]),
        deliverable("analysis", "Explain findings", "writing", dependencies=["profile"]),
    ], {"baseline": {"code": MEASUREMENT_CODE}})
    original = await_task(service, service.complete_assignment("2", "retry-original")["id"])
    supplement = {"unit_id": "profile", "mode": "supplement", "notes": "Saved readings.",
                  "files": [upload("readings.csv", b"x,y\n1,4\n2,6\n")], "approved": True}
    ai.responses["profile"] = AIError("EMPTY_MODEL_RESPONSE")
    parent = await_task(service, service.revise_completion(original["id"], supplement, "retry-supplement")["id"])
    assert parent["status"] == "waiting_review"
    assert parent["completion"]["status"] == "needs_action"
    assert parent["tool_results"]["unit-profile"]["error_code"] == "EMPTY_MODEL_RESPONSE"
    ai.responses.update(profile=section("Recovered profile."), analysis=section("Recovered analysis."))
    ai.calls.clear()

    client = TestClient(create_app(service, "retry-key"), base_url=ORIGIN)
    client.get("/unlock/retry-key", follow_redirects=False)
    path = f"/api/tasks/{parent['id']}/revise"
    headers = {"Idempotency-Key": "retry-stage"}
    body = {"unit_id": "profile", "mode": "retry", "approved": True}
    invalid = client.post(path, json={**body, "notes": "Extra notes."}, headers=headers)
    assert invalid.status_code == 400 and invalid.json()["error"]["code"] == "INVALID_REQUEST"
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 202, response.json()
    result = await_task(service, response.json()["data"]["id"])

    assert result["status"] == "waiting_review", result.get("error")
    assert [step["id"] for step in result["plan"]["steps"][:-1]] == ["unit-profile", "unit-analysis"]
    assert [call["deliverable"]["id"] for call in ai.calls] == ["profile", "analysis"]
    saved = ai.calls[0]["user_supplements"][0]
    assert saved["notes"] == supplement["notes"] and saved["files"][0]["excerpt"] == "x,y\n1,4\n2,6\n"
    assert result["tool_results"]["unit-baseline"]["run_id"] == parent["tool_results"]["unit-baseline"]["run_id"]
    assert len(runner.calls) == 1
    assert "Recovered analysis." in result["draft"]
    assert service.get("task", parent["id"]) == parent
    assert client.post(path, json=body, headers=headers).json()["data"]["id"] == result["id"]
