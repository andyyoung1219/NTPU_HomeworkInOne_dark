"""Create a review revision using the saved deliverable dependency graph."""

import base64
import binascii
import hashlib
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from backend.agent.completion import profile_csv
from backend.agent.completion_units import (
    deliverable_step,
    validate_artifact_content,
    verified_bytes,
)
from backend.agent.contracts import digest, fail
from backend.agent.runner import filesystem_path
from backend.models.store import now

MAX_UPLOAD_BYTES = 1_000_000
MAX_UPLOAD_ENCODED = 4 * ((MAX_UPLOAD_BYTES + 2) // 3)
MAX_REVISION_FILES = 12
MAX_SUPPLEMENT_FILES = 4
TEXT_SUFFIXES = {".txt", ".csv", ".json"}
RESULT_SUFFIXES = TEXT_SUFFIXES | {".png"}


def affected_units(units, selected):
    affected = {selected}
    for unit in units:  # Validated plans store deliverables in topological order.
        if affected.intersection(unit["dependencies"] + unit.get("optional_dependencies", [])):
            affected.add(unit["id"])
    return [unit for unit in units if unit["id"] in affected]


def read_uploads(body, unit):
    from backend.services.study import StudyError

    if body["mode"] == "retry":
        if body["files"] or body["notes"].strip():
            fail("INVALID_REQUEST")
        return {}
    result = body["mode"] == "result"
    if not result and len(body["files"]) > MAX_SUPPLEMENT_FILES:
        fail("REVISION_FILE_INVALID")
    uploads = {}
    for item in body["files"]:
        name = item["name"]
        suffix = Path(name).suffix.lower()
        if (not re.fullmatch(r"[\w][\w .()-]{0,119}", name) or
                suffix not in (RESULT_SUFFIXES if result else TEXT_SUFFIXES) or
                any(old.casefold() == name.casefold() for old in uploads)):
            fail("REVISION_FILE_INVALID")
        try:
            data = base64.b64decode(item["data"], validate=True)
        except (binascii.Error, ValueError):
            fail("REVISION_FILE_INVALID")
        if not data or len(data) > MAX_UPLOAD_BYTES:
            fail("REVISION_FILE_INVALID")
        try:
            validate_artifact_content(suffix, data)
            if suffix != ".png":
                data.decode("utf-8-sig")
        except (StudyError, UnicodeError):
            fail("REVISION_FILE_INVALID")
        uploads[name] = data
    if result and set(unit["expected_artifacts"]) - uploads.keys():
        fail("REVISION_RESULTS_MISSING")
    if not uploads and not body["notes"].strip():
        fail("REVISION_INPUT_REQUIRED")
    return uploads


def read_description(workspace, expected):
    path = workspace / "input/assignment-description.txt"
    if not path.is_file() or not path.resolve().is_relative_to(workspace.resolve()):
        fail("ASSET_CHANGED")
    raw = filesystem_path(path).read_bytes()
    # Older direct-completion snapshots may have used text-mode CRLF on Windows.
    if hashlib.sha256(raw).hexdigest() != expected and (
            hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest() != expected):
        fail("ASSET_CHANGED")
    return raw


def verify_revision_source(study, user, task, ref, expected):
    workspace = study.workspace(user, task["id"])
    if ref == "assignment-description":
        read_description(workspace, expected)
    elif ref.startswith("dataset:"):
        dataset = next((item for item in task["completion_data"]["datasets"] if item["file_id"] == ref), None)
        if not dataset or dataset["sha256"] != expected:
            fail("ASSET_CHANGED")
        verified_bytes(study, workspace, dataset, max_bytes=2_000_000)
    else:
        suffix = Path(task["source_names"][ref]).suffix.lower()
        path = workspace / "input" / (expected + suffix)
        verified_bytes(study, workspace, {"path": str(path.relative_to(study.root)), "sha256": expected},
                       max_bytes=10_000_000)


def copy_snapshot(study, parent, workspace, task, replaced_unit):
    """Copy retained evidence into the new task and rebind its file references."""
    from backend.services.study import atomic_text

    parent = parent.resolve()
    copied = {}

    def copy(artifact):
        source = study.safe_path(artifact["path"])
        if not source.is_relative_to(parent):
            fail("PATH_OUTSIDE_WORKSPACE")
        if source not in copied:
            data = verified_bytes(study, parent, artifact, max_bytes=10_000_000)
            target = workspace / source.relative_to(parent)
            disk_target = filesystem_path(target)
            disk_target.parent.mkdir(parents=True, exist_ok=True)
            disk_target.write_bytes(data)
            copied[source] = (str(target.relative_to(study.root)), artifact["sha256"])
        path, expected = copied[source]
        if artifact["sha256"] != expected:
            fail("ARTIFACT_CHANGED")
        return path

    def rebind(value):
        if isinstance(value, dict):
            if isinstance(value.get("path"), str) and isinstance(value.get("sha256"), str):
                value["path"] = copy(value)
            for nested in value.values():
                rebind(nested)
        elif isinstance(value, list):
            for nested in value:
                rebind(nested)

    for asset in task["completion_inputs"]["assets"]:
        suffix = Path(asset["name"]).suffix.lower()
        source = parent / "input" / (asset["sha256"] + suffix)
        copy({"path": str(source.relative_to(study.root.resolve())), "sha256": asset["sha256"]})
    if expected := task.get("source_context_sha256"):
        source = parent / "input/source-context.json"
        copy({"path": str(source.relative_to(study.root.resolve())), "sha256": expected})
    if expected := task["source_hashes"].get("assignment-description"):
        atomic_text(workspace / "input/assignment-description.txt", read_description(parent, expected).decode("utf-8"))
    for item in task.get("unit_supplements", {}).pop(replaced_unit, []):
        for artifact in item["files"]:
            task["source_names"].pop(artifact["file_id"], None)
            task["source_hashes"].pop(artifact["file_id"], None)
    for key in ("tool_results", "deliverable_execution", "runs", "completion_data", "source_images",
                "visual_input", "unit_supplements"):
        rebind(task.get(key))


def save_uploads(study, task, workspace, body, uploads):
    notes = body["notes"].strip()
    result = body["mode"] == "result"
    unit_id = body["unit_id"]
    artifacts, profiles = [], []
    for index, (name, data) in enumerate(uploads.items(), 1):
        sha = hashlib.sha256(data).hexdigest()
        suffix = Path(name).suffix.lower()
        target = workspace / "input" / (sha + suffix)
        disk_target = filesystem_path(target)
        disk_target.parent.mkdir(parents=True, exist_ok=True)
        disk_target.write_bytes(data)
        artifact = {"filename": name, "path": str(target.relative_to(study.root)), "sha256": sha}
        if not result:
            ref = f"revision:{task['id']}:{index}"
            artifact["file_id"] = ref
            task["source_hashes"][ref] = sha
            task["source_names"][ref] = name
            if suffix == ".csv":
                first_line = data.partition(b"\n")[0]
                delimiter = ";" if first_line.count(b";") > first_line.count(b",") else ","
                profiles.append({"file_id": ref, "sandbox_path": "/input/" + target.name,
                                 "profile": profile_csv(data, delimiter)})
        artifacts.append(artifact)
    if result:
        files = {}
        for index, artifact in enumerate(artifacts, 1):
            name = artifact["filename"]
            if not re.fullmatch(r"[\w.-]+", name) or name.casefold() == "run.json":
                name = f"supplied_{index}{Path(name).suffix.lower()}"
            files[f"{unit_id}__{index}_{name}"] = artifact
        task["revision"]["supplied_result"] = {"status": "user_supplied", "notes": notes, "artifacts": files}
    else:
        task.setdefault("unit_supplements", {})[unit_id] = [
            {"notes": notes, "files": artifacts, "csv_profiles": profiles}]


def save_draft_edits(study, task, workspace):
    """Snapshot edits from the parent for local report assembly and resume."""
    from backend.services.study import atomic_text

    previous_snapshot = task.pop("revision_draft", None)
    report_model = task.get("report_model")
    if not report_model or not (task.get("edited_at") or previous_snapshot):
        return None
    original, _ = study.format_result(json.dumps(report_model, ensure_ascii=False), task)
    edited = task["draft"].replace("\r\n", "\n")
    if original == edited:
        return None
    path = workspace / "input/revision-draft.json"
    content = json.dumps({"original": original, "edited": edited}, ensure_ascii=False)
    atomic_text(filesystem_path(path), content)
    return {"path": str(path.relative_to(study.root)), "sha256": hashlib.sha256(content.encode()).hexdigest()}


def start_revision(study, parent_id, body, idempotency_key):
    if not body["approved"]:
        fail("REVISION_APPROVAL_REQUIRED")
    if not idempotency_key or len(idempotency_key) > 100:
        fail("IDEMPOTENCY_KEY_REQUIRED")
    with study.lock:
        user = study.require_user()
        identifier = uuid.uuid5(uuid.NAMESPACE_URL, f"revision:{user}:{idempotency_key}").hex
        if study.store.get(user, "draft_deletion", identifier):
            fail("NOT_FOUND_OR_NOT_AUTHORIZED")
        signature = digest({"parent_id": parent_id, "body": body})
        if existing := study.store.get(user, "task", identifier):
            if existing["request_sha256"] != signature:
                fail("IDEMPOTENCY_CONFLICT")
            return existing
        task = study.get("task", parent_id, user)
        plan = task.get("plan", {})
        units = plan.get("deliverables")
        if (not units or task["request"]["mode"] != "complete_assignment"
                or task["status"] not in {"waiting_review", "completed"} or not task["draft"]):
            fail("REVISION_NOT_AVAILABLE")
        if digest(plan) != task["plan_sha256"]:
            fail("PLAN_CHANGED")
        selected = next((unit for unit in units if unit["id"] == body["unit_id"]), None)
        if selected is None:
            fail("REVISION_STAGE_INVALID")
        uploads = read_uploads(body, selected)
        affected = affected_units(units, selected["id"])
        affected_ids = {unit["id"] for unit in affected}
        affected_steps = {"unit-" + unit_id for unit_id in affected_ids}
        report_step = next(step for step in plan["steps"] if step["tool"] == "generate_report")
        workspace = study.workspace(user, identifier)
        if study.active_jobs or not study.ai_lock.acquire(blocking=False):
            fail("AI_BUSY")
        saved = False
        try:
            draft_snapshot = save_draft_edits(study, task, workspace)
            coverage = {item["deliverable_id"]: item for item in task.get("coverage", [])
                        if item.get("deliverable_id")}
            task["tool_results"] = {step: result for step, result in task["tool_results"].items()
                                    if step not in affected_steps and step != report_step["id"]}
            for unit in units:
                if unit["id"] not in affected_ids:
                    step = "unit-" + unit["id"]
                    task["tool_results"].setdefault(step, {
                        "status": "needs_action", "reasons": coverage.get(unit["id"], {}).get("reasons", [])})
            task["deliverable_execution"] = {
                unit: state for unit, state in task.get("deliverable_execution", {}).items()
                if unit not in affected_ids}
            retained_runs = {state["result"]["run_id"] for state in task["deliverable_execution"].values()
                             if state.get("result")}
            task["runs"] = [run for run in task.get("runs", []) if run["run_id"] in retained_runs]
            task["tool_audit"] = [item for item in task["tool_audit"]
                                  if item["step_id"] not in affected_steps | {report_step["id"], "human-export"}]
            reused_audit = [item for item in task["tool_audit"] if item.get("result_sha256")
                            and item["step_id"] in task["tool_results"]
                            and item["result_sha256"] == digest(task["tool_results"][item["step_id"]])]
            for key in ("pdf", "qa", "exports", "export_recovery", "generated_files", "edited_at",
                        "model_task_id", "report_model", "coverage"):
                task.pop(key, None)
            plan["steps"] = [deliverable_step(unit) for unit in affected] + [report_step]
            task.update(id=identifier, created_at=now(), status="running", draft="", error=None,
                        reviewed=False, ready_for_export=False, accepted_incomplete=False, blockers=[],
                        completion={"status": "running", "blockers": []}, request_sha256=signature,
                        plan_sha256=digest(plan), deadline_at=time.time() + plan["limits"]["deadline_seconds"],
                        checkpoint={"cursor": 0, "attempts": {}, "steps_used": 0, "requests_used": 0,
                                    "prompt_chars_used": 0, "in_flight": None, "consecutive_errors": 0},
                        revision={"parent_task_id": parent_id, "unit_id": selected["id"], "mode": body["mode"],
                                  "affected_unit_ids": [unit["id"] for unit in affected]})
            task["approvals"] = {"high": True, "external": False, "snapshot": signature, "approved_at": now()}
            if draft_snapshot:
                task["revision_draft"] = draft_snapshot
            copy_snapshot(study, study.workspace(user, parent_id), workspace, task,
                          selected["id"] if body["mode"] == "supplement" else None)
            for item in reused_audit:
                item.update(reused_from_task_id=parent_id,
                            result_sha256=digest(task["tool_results"][item["step_id"]]))
            if body["mode"] != "retry":
                save_uploads(study, task, workspace, body, uploads)
            study.store.save(user, "task", identifier, task)
            saved = True
            study.store.event(user, identifier, "running")
            study.active_jobs[identifier] = {"cancel": threading.Event(), "bridge_id": None}
            threading.Thread(target=study._run, args=(user, task), daemon=True).start()
        except Exception:
            study.active_jobs.pop(identifier, None)
            study.ai_lock.release()
            if saved:
                task.update(status="failed", error="COMPLETION_START_FAILED")
                study.store.save(user, "task", identifier, task)
            elif workspace.resolve().is_relative_to(study.root.resolve() / "workspaces" / user):
                shutil.rmtree(filesystem_path(workspace), ignore_errors=True)
            raise
        return study.store.get(user, "task", identifier)
