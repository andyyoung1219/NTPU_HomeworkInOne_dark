"""Run and assemble independently checkable direct-completion deliverables."""

import ast
import hashlib
import io
import json
import re
import sys
import time
from bisect import bisect_right
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image

from backend.agent.completion_context import (
    format_grounded_action,
    grounded_actions,
    unit_context,
    wire_width,
)
from backend.agent.completion_fallback import dataset_proposal
from backend.agent.contracts import REGISTRY, digest, fail, validate_step
from backend.agent.deliverables import mandatory_unit_requirements, unit_blockers
from backend.agent.runner import filesystem_path
from backend.services.documents import optional_requirement

RECOVERABLE_UNIT_ERRORS = {
    "AI_TIMEOUT", "AI_INVALID_REPORT_SCHEMA", "AI_INVALID_COMPLETION_PLAN", "TOOL_TIMEOUT",
    "AI_BRIDGE_ERROR", "AI_BRIDGE_EXITED", "AI_CONNECTION_PENDING",
    "OPERATION_TIMEOUT", "INCOMPLETE_MODEL_RESPONSE", "EMPTY_MODEL_RESPONSE",
    "PYTHON_EXECUTION_FAILED", "SANDBOX_NOT_AVAILABLE", "SANDBOX_EXECUTION_FAILED",
    "RUNNER_OUTPUT_LIMIT", "RUNNER_RESULT_INVALID", "DELIVERABLE_ARTIFACT_MISSING",
    "DELIVERABLE_ARTIFACT_INVALID", "AI_PROMPT_TOO_LARGE",
}
PYTHON_VALIDATION_SCOPE = "execution_and_file_format"
MAX_SECTION_CHARS = 16_000
WRITING_RECOVERY_ERRORS = {"AI_INVALID_REPORT_SCHEMA", "INCOMPLETE_MODEL_RESPONSE", "EMPTY_MODEL_RESPONSE"}
COMPLETION_LIMITS = {
    "TASK_DEADLINE_EXCEEDED", "STEP_BUDGET_EXCEEDED", "RETRY_BUDGET_EXCEEDED",
    "REQUEST_BUDGET_EXCEEDED", "PROMPT_BUDGET_EXCEEDED",
}
COMPACT_RETRY_ERRORS = {
    "AI_PROMPT_TOO_LARGE", "AI_TIMEOUT", "AI_BRIDGE_ERROR", "AI_BRIDGE_EXITED",
    "TOOL_TIMEOUT", "INCOMPLETE_MODEL_RESPONSE", "EMPTY_MODEL_RESPONSE", "AI_INVALID_REPORT_SCHEMA", "COMPLETION_INCOMPLETE",
}


def deliverable_step(unit):
    return validate_step({"id": "unit-" + unit["id"], "objective": unit["title"],
                          "tool": "complete_deliverable", "required_inputs": {"unit_id": unit["id"]},
                          "expected_artifacts": unit["expected_artifacts"],
                          "approval_level": REGISTRY["complete_deliverable"][0]})


def expand_plan(task, units):
    """Insert only server-defined tools after the planning checkpoint."""
    if not units:
        return
    plan = task["plan"]
    steps = [deliverable_step(unit) for unit in units]
    index = next(i for i, step in enumerate(plan["steps"]) if step["tool"] == "execute_completion")
    plan["steps"][index:index + 1] = steps
    plan["deliverables"] = units
    task["plan_sha256"] = digest(plan)


def lookup(task, unit_id):
    return next(unit for unit in task["plan"]["deliverables"] if unit["id"] == unit_id)


def localized(task, en, zh):
    return en if task["completion_plan"].get("report_language") == "en" else zh


def mark_action(reasons):
    return {"status": "needs_action", "reasons": reasons}


def fallback_unit(task, unit_id, code, *, engine=None, user=None):
    if code not in RECOVERABLE_UNIT_ERRORS:
        return None
    state = task.get("deliverable_execution", {}).get(unit_id, {})
    reason = state.get("failure", {}).get("message")
    if code == "AI_PROMPT_TOO_LARGE":
        reason = localized(task, "Split this deliverable or provide a shorter source excerpt before retrying.",
                           "請拆分這項成果，或提供較短的來源節錄後再試。")
    if code in {"AI_BRIDGE_ERROR", "AI_BRIDGE_EXITED", "AI_CONNECTION_PENDING"}:
        reason = localized(task, "Check the NTPU AI connection and retry, or finish this deliverable manually.",
                           "請檢查 NTPU AI 連線後重試，或手動完成這項成果。")
    if code == "RUNNER_OUTPUT_LIMIT":
        reason = localized(task, "Reduce the program output size or split the files before running it again.",
                           "請縮小程式輸出或拆分檔案後再執行。")
    reason = reason or localized(
        task, f"Complete and verify this deliverable separately ({code}).",
        f"請另行完成並驗證此項成果（{code}）。",
    )
    if engine is not None and code in WRITING_RECOVERY_ERRORS:
        if recovered := dataset_proposal(engine, user, task, lookup(task, unit_id)):
            recovery = localized(task, "Review the proposal recovered from the available dataset and complete any missing interpretation.",
                                 "請審閱根據現有資料集重建的提案，並補上尚缺的解讀。")
            return {**mark_action([recovery]), **recovered, "degraded": True, "error_code": code}
    return {**mark_action([reason]), "degraded": True, "error_code": code}


def verified_bytes(study, workspace, artifact, max_bytes=1_000_000):
    path = study.safe_path(artifact["path"])
    if not path.resolve().is_relative_to(workspace.resolve()):
        fail("PATH_OUTSIDE_WORKSPACE")
    path = filesystem_path(path)
    if not path.is_file() or path.stat().st_size > max_bytes:
        fail("ARTIFACT_CHANGED")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
        fail("ARTIFACT_CHANGED")
    return data


def model_data(data, unit, compact, writing=False, proposal=False):
    """Keep calculated statistics and selected real records within the request budget."""
    selected = set(unit["source_ids"])
    keys = ("datasets", "attachment_profiles")
    relevant = selected & {entry["file_id"] for key in keys for entry in data.get(key, [])}
    inferred_profiles = {"csv-profiling", "public-uci-dataset"}.intersection(unit["required_capabilities"])
    result = {}
    for key in keys:
        entries = [entry for entry in data.get(key, []) if entry["file_id"] in relevant or
                   (not relevant and (not selected or inferred_profiles or proposal))]
        count = 2 if compact else 4
        result[key] = []
        for entry in entries[:count]:
            profile = entry["profile"]
            columns = profile["columns"]
            limit = (12 if writing else 8) if compact else 20
            details = [column for column in columns if column and len(column) <= 120][:limit]
            if len(details) < len(columns):
                result["profiles_limited"] = True
            record = {
                **{field: entry[field] for field in ("file_id", "title", "url", "page", "license", "description",
                                                     "delimiter", "sandbox_path") if field in entry},
                "profile": {"records": profile["records"], "column_count": len(columns), "columns": details,
                            "attributes": {column: {name: value for name, value in profile["attributes"][column].items()
                                                    if name != "frequencies" or (not compact and
                                                        len(json.dumps(value, ensure_ascii=False)) <= 250)}
                                           for column in details}},
            }
            if writing:
                examples = [{column: str(row[column]) for column in details if row.get(column) is not None}
                            for row in profile.get("examples", [])[:5]
                            if all(len(str(row.get(column, ""))) <= 80 for column in details)]
                while examples and wire_width(json.dumps(examples, ensure_ascii=False)) > (2000 if compact else 3300):
                    examples.pop()
                record["profile"]["examples"] = examples
            result[key].append(record)
        if len(entries) > count:
            result["profiles_limited"] = True
    return result


def execute_unit(engine, user, task, unit_id):
    from backend.agent.completion import source_snapshot

    workspace = engine.study.workspace(user, task["id"])
    revision = task.get("revision", {})
    if revision.get("unit_id") == unit_id and revision["mode"] == "result":
        result = revision["supplied_result"]
        for artifact in result["artifacts"].values():
            verified_bytes(engine.study, workspace, artifact)
        return result

    unit = lookup(task, unit_id)
    links = unit["dependencies"] + unit.get("optional_dependencies", [])
    dependencies = {ref: task["tool_results"].get("unit-" + ref, {}) for ref in links}
    available = {"verified", "ready_for_review", "user_supplied"}
    supplements = task.get("unit_supplements", {}).get(unit_id, [])
    profiles = [profile for item in supplements for profile in item["csv_profiles"]]
    blockers = unit_blockers(unit, task, runner_available=True, supplemental_csv=bool(profiles))
    blocked_by = [ref for ref in unit["dependencies"] if dependencies[ref].get("status") not in available]
    if (unit["kind"] == "writing" and "python-standard-library" in unit["required_capabilities"]
            and not blocked_by
            and not any(result.get("status") in {"verified", "user_supplied"} for result in dependencies.values())):
        blockers.append(localized(task, "Provide recorded execution results for this section.",
                                  "請提供有執行紀錄的程式結果，再完成這個章節。"))
    if blocked_by and not blockers:
        reasons = [localized(task, f"First finish the required deliverable {lookup(task, ref)['title']}.",
                             f"請先完成前置成果「{lookup(task, ref)['title']}」。") for ref in blocked_by]
        return {**mark_action(reasons), "blocked_by": blocked_by}
    if blockers:
        return mark_action(blockers)
    previous_errors = [audit["error_code"] for audit in task["tool_audit"]
                       if audit["step_id"] == "unit-" + unit_id and audit.get("error_code")]
    compact = bool(COMPACT_RETRY_ERRORS.intersection(previous_errors))
    context = unit_context(source_snapshot(engine, user, task), unit, compact=compact)
    if context["missing_locations"] and not supplements:
        missing = [f"{ref['file_id']} / {ref.get('page', ref.get('section'))}" for ref in context["missing_locations"]]
        locations = ", ".join(missing)
        return mark_action([localized(
            task, f"The required source pages or sections could not be read ({locations}). Supply their contents to finish this deliverable.",
            f"指定的來源頁節無法讀取（{locations}）。請補上該頁節內容再完成此項成果。",
        )])
    if unit["kind"] == "python":
        if not task["approvals"].get("high"):
            fail("TOOL_APPROVAL_REQUIRED")

    files, evidence = {}, []
    writing = unit["kind"] == "writing"
    evidence_remaining = (4800 if compact else 9500) if writing else 8500
    for ref, result in dependencies.items():
        if result.get("status") not in available:
            evidence.append({"deliverable": ref, "status": "needs_action"})
            continue
        summary = {"deliverable": ref, "status": result["status"]}
        if result["status"] in {"verified", "user_supplied"}:
            summary["files"] = []
            if result["status"] == "verified":
                summary["validation_scope"] = result.get("validation_scope", PYTHON_VALIDATION_SCOPE)
                summary["stdout"] = result["stdout"][:(500 if compact else 1200)]
            else:
                summary["user_supplied_notes"] = result["notes"][:(500 if compact else 2000)]
            for artifact in result["artifacts"].values():
                if result["status"] == "verified" and artifact["filename"] == "run.json":
                    continue
                data = verified_bytes(engine.study, workspace, artifact)
                suffix = Path(artifact["filename"]).suffix.lower()
                key = artifact["sha256"] + suffix
                files[key] = data
                entry = {"name": artifact["filename"], "sandbox_path": "/input/" + key}
                if suffix in {".json", ".csv", ".txt"}:
                    per_file = (3000 if compact else 5500) if writing and suffix == ".csv" else (140 if compact else 450)
                    limit = min(per_file, evidence_remaining)
                    if limit:
                        excerpt = data[:limit].decode("utf-8-sig", errors="replace")
                        entry["excerpt"] = excerpt
                        entry["complete_text"] = len(data) <= limit
                        evidence_remaining = max(0, evidence_remaining - wire_width(excerpt))
                summary["files"].append(entry)
        else:
            summary["draft_context"] = result["content"][:(400 if compact else 1200)]
        evidence.append(summary)

    additional = []
    for item in supplements:
        supplement = {"notes": item["notes"][:(500 if compact else 2000)], "files": []}
        for artifact in item["files"]:
            data = verified_bytes(engine.study, workspace, artifact)
            filename = artifact["sha256"] + Path(artifact["filename"]).suffix.lower()
            files[filename] = data
            supplement["files"].append({"name": artifact["filename"], "file_id": artifact["file_id"],
                                       "sandbox_path": "/input/" + filename,
                                       "excerpt": data.decode("utf-8-sig")[:(450 if compact else 1200)]})
        additional.append(supplement)
    data = task["completion_data"]
    if profiles:
        data = {**data, "attachment_profiles": profiles + data.get("attachment_profiles", [])}
    scoped_unit = {**unit, "source_ids": list(dict.fromkeys(unit["source_ids"] + [
        artifact["file_id"] for item in supplements for artifact in item["files"]]))}
    deliverable_type = task["completion_plan"].get("deliverable_type")
    payload = {"assignment": task["title"], "deliverable": unit,
               "inputs": context, "dependencies": evidence,
               "data": model_data(data, scoped_unit, compact, writing=writing,
                                  proposal=writing and deliverable_type == "proposal"),
               "deliverable_type": deliverable_type,
               "compact_context": compact, "previous_error": previous_errors[-1] if previous_errors else None,
               "report_language": task["completion_plan"].get("report_language"),
               "allowed_sources": list(task["source_hashes"])}
    if additional:
        payload["user_supplements"] = additional
    if unit["kind"] == "python":
        return execute_python(engine, user, task, unit, workspace, files, payload)
    return draft_section(engine, user, task, unit, payload)


def draft_section(engine, user, task, unit, payload):
    from backend.agent.completion import WRITING_RETRY_ERRORS, ask, markdown_recovery_instruction

    instruction = (
        '只撰寫指定 deliverable，回覆 {"content":"完整、可交付的正文 Markdown","sources":["實際使用的 file_id"],'
        '"required_actions":[{"requirement_id":"REQ-01","quote":"逐字引用未完成的必交要求"}]}。依題目指定語言撰寫。引用限 allowed_sources，'
        "只依來源及前置產物陳述測量結果，user_supplied 是使用者提供的結果，須標明來源；draft_context 僅供行文參考。"
        "execution_and_file_format 表示程式已執行且檔案通過格式檢查，須由審閱者核對數值與解法。"
        "user_supplements 是使用者為本節補上的說明與資料，引用所用檔案的 file_id。"
        "各資料切分使用其指定指標。題目未要求的指標、比較、選做或延伸建議可留在正文，切勿列為缺漏。"
        "若前置成果仍待處理，正文只說明已有的結果與限制，由前置單位負責其待辦。"
        "工作台另行顯示使用者待辦，正文不要另列待辦清單。"
        "required_actions 只收本節直接負責且仍未完成的必交要求，每個 requirement_id 最多一筆。"
        "quote 必須從 inputs.requirements 的對應文字逐字摘錄未完成的條件，不添加新指標。沒有待辦可省略 required_actions。"
        "提案應完整展開資料、研究問題、具體方法與評估、預期發現、應用及限制。用實際 profile.examples 或前置 CSV 呈現樣本，"
        "資料集直接下載網址與來源頁要分清楚。若私人雲端連結未提供，只寫已知的完整資料連結。以未來式表達未執行分析。"
        "優先完整正文；以 JSON 字串的跳脫換行或 sections 陣列輸出，正文最多 16000 字。"
    )
    if payload.get("previous_error") in WRITING_RETRY_ERRORS:
        result = ask(engine, user, task, markdown_recovery_instruction(instruction), payload, markdown=True)
    else:
        result = ask(engine, user, task, instruction, payload)
    content = section_content(result)
    if not content or len(content) > MAX_SECTION_CHARS:
        fail("AI_INVALID_REPORT_SCHEMA")
    reported_sources = result.get("sources", [])
    reported_sources = [reported_sources] if isinstance(reported_sources, str) else reported_sources
    if not isinstance(reported_sources, list):
        reported_sources = []
    sources = [ref for ref in reported_sources if isinstance(ref, str) and ref in task["source_hashes"]]
    actions = result.get("required_actions", [])
    if actions is None:
        actions = []
    if not isinstance(actions, list) or len(actions) > 30:
        actions = []
    assigned = set(unit.get("requirement_ids", []))
    for dependency in payload["dependencies"]:
        if dependency["status"] == "needs_action":
            assigned.difference_update(lookup(task, dependency["deliverable"]).get("requirement_ids", []))
    requirements = [item for item in task["completion_inputs"]["requirements"] if item["id"] in assigned]
    required_actions = grounded_actions(requirements, actions)
    reasons = [format_grounded_action(ref, quote, task["completion_plan"].get("report_language") == "en")
               for ref, quote in required_actions.items()]
    if re.fullmatch(r"(?:[^:\n.。;；]{0,80}[:：]\s*)?(?:TODO|TBD|待填)[\s.。;；]*", content.strip(), re.I):
        fail("AI_INVALID_REPORT_SCHEMA")
    if not task.get("source_grounded_actions") and re.search(r"\bTODO\b|\bTBD\b|待填", content, re.I):
        reasons.append(localized(task, "Fill the missing content in this section.",
                                  "請補齊本節標示待填的內容。"))
    required_sources = set(unit["source_ids"]) - {"assignment-description"}
    if payload.get("deliverable_type") == "proposal":
        required_sources.update(dataset["file_id"] for dataset in payload["data"].get("datasets", []))
        sources = [ref for ref in sources if ref != "assignment-description"]
    for ref in sorted(required_sources):
        if ref not in sources:
            name = task["source_names"].get(ref, ref)
            reasons.append(localized(task, f"Verify and cite the required source {name}.",
                                      f"請核對並引用指定來源「{name}」。"))
    # Preserve a reported omission for review when the unit has no requirement mapping.
    if (task.get("source_grounded_actions") and not unit.get("optional")
            and not unit.get("requirement_ids") and actions and not reasons):
        reasons.append(localized(task, "The draft reports an omission without a matching assignment requirement.",
                                  "草稿回報的缺漏尚未對應到題目要求。"))
    status = mark_action(reasons) if reasons else {"status": "ready_for_review"}
    if result.get("markdown_recovered") is True:
        status["review_notice"] = localized(task,
            "This section was recovered from Markdown. Review its coverage and source citations.",
            "本節從 Markdown 回覆復原，請核對題目涵蓋範圍與引文。")
    return {**status, "content": content.strip(), "sources": list(dict.fromkeys(sources))}


def section_content(result):
    if isinstance(content := result.get("content"), str) and content.strip():
        return content.strip()
    sections = result.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= 30:
        return ""
    parts = []
    for section in sections:
        if not isinstance(section, dict) or not isinstance(content := section.get("content"), str) or not content.strip():
            return ""
        heading = section.get("heading")
        if isinstance(heading, str) and heading.strip():
            parts.append("### " + " ".join(heading.split()))
        parts.append(content.strip())
    return "\n\n".join(parts)


def validate_artifact_content(name, data):
    try:
        if name.endswith(".png"):
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or image.width * image.height > 20_000_000:
                    fail("DELIVERABLE_ARTIFACT_INVALID")
                image.verify()
        elif name.endswith(".json"):
            json.loads(data)
        elif not data.decode("utf-8-sig").strip():
            fail("DELIVERABLE_ARTIFACT_INVALID")
    except (ValueError, OSError, SyntaxError, Image.DecompressionBombError):
        fail("DELIVERABLE_ARTIFACT_INVALID")


def execute_python(engine, user, task, unit, workspace, files, payload):
    from backend.agent.completion import ask
    from backend.services.study import StudyError

    state = task.setdefault("deliverable_execution", {}).setdefault(unit["id"], {})
    inputs = engine.inputs(user, task)
    for dataset in task["completion_data"]["datasets"]:
        data = verified_bytes(engine.study, workspace, dataset, max_bytes=2_000_000)
        inputs[dataset["sha256"] + ".csv"] = data
    inputs.update(files)
    input_hashes = {name: hashlib.sha256(data).hexdigest() for name, data in inputs.items()}
    if result := state.get("result"):
        verified_bytes(engine.study, workspace, state["program"])
        if result["source_hashes"] != input_hashes:
            fail("ASSET_CHANGED")
        for artifact in result["artifacts"].values():
            verified_bytes(engine.study, workspace, artifact)
        return result
    previous_code = verified_bytes(engine.study, workspace, state["program"]).decode() if state.get("program") else None
    code = previous_code
    if not code or state.get("failure"):
        response = ask(engine, user, task, (
            '只為指定 deliverable 回覆 {"code":"可執行的 Python 程式"}。使用 Python 標準函式庫，'
            "只讀 input_files 與前置成果提供的 /input 檔案。若有 user_supplements，依補充說明使用其 sandbox_path。"
            "執行所需檢查並在 /output 產生指定 expected_artifacts，stdout 列出精簡結果。"
            "不得安裝套件或連網。程式最多 12000 字。"
        ), {**payload, "previous_code": previous_code, "previous_failure": state.get("failure"),
            "input_files": ["/input/" + name for name in inputs]})
        code = response.get("code")
        if not isinstance(code, str) or not code.strip() or len(code) > 12000:
            fail("AI_INVALID_COMPLETION_PLAN")
        code = code.replace("\r\n", "\n").replace("\r", "\n")
        try:
            tree = ast.parse(code)
        except SyntaxError as error:
            state["failure"] = {"syntax_error": error.msg[:400], "line": error.lineno,
                                "source_line": (error.text or "")[:400]}
            fail("AI_INVALID_COMPLETION_PLAN")
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                modules.add(node.module.split(".")[0])
        data = code.encode("utf-8")
        sha = hashlib.sha256(data).hexdigest()
        target = workspace / "work" / "deliverables" / unit["id"] / (sha + ".py")
        disk_path = filesystem_path(target)
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(data)
        state["program"] = {"path": str(target.relative_to(engine.study.root)), "sha256": sha}
        state.pop("failure", None)
        missing = ", ".join(sorted(modules - sys.stdlib_module_names))
        if missing:
            reason = localized(task, f"Run or adapt the generated program in an environment with {missing}.",
                               f"請在具備 {missing} 的環境執行已產生的程式，或調整程式後再驗證。")
            state["failure"] = {"message": reason, "missing_imports": missing}
            engine.save(user, task)
            fail("PYTHON_EXECUTION_FAILED")
        engine.save(user, task)

    engine.check(user, task)
    if not engine.runner.status()["available"]:
        reason = localized(task,
            "The Python program is available for download. Run it in the isolated Docker Runner or supply the actual results.",
            "Python 程式已可下載，請在隔離 Docker Runner 執行或補上實際結果。")
        return mark_action([reason])
    result = engine.runner.run(code, inputs, workspace / "work" / "runs",
                               engine.study.active_jobs[task["id"]]["cancel"],
                               min(task["plan"]["limits"]["tool_timeout"], task["deadline_at"] - time.time()))
    if result["code_sha256"] != state["program"]["sha256"]:
        fail("CODE_CHANGED")
    if result["source_hashes"] != input_hashes:
        fail("ASSET_CHANGED")
    for artifact in result["artifacts"].values():
        artifact["path"] = str(Path(artifact["path"]).relative_to(engine.study.root))
        verified_bytes(engine.study, workspace, artifact)
    task.setdefault("runs", []).append(result)
    if result["exit_code"]:
        exit_code = result["exit_code"]
        state["failure"] = {"exit_code": exit_code, "stdout": result["stdout"][:3000], "message": localized(
            task, f"Download and fix the generated program, then rerun it (exit code {exit_code}).",
            f"請下載並修正程式後重新執行（結束碼 {exit_code}）。",
        )}
        fail("PYTHON_EXECUTION_FAILED")
    missing = sorted(set(unit["expected_artifacts"]) - result["artifacts"].keys())
    if missing:
        files = ", ".join(missing)
        state["failure"] = {"missing_artifacts": missing, "message": localized(
            task, f"Produce and verify the missing files: {files}.", f"請補產生並驗證缺少的檔案 {files}。")}
        fail("DELIVERABLE_ARTIFACT_MISSING")
    artifacts = {}
    for name, artifact in result["artifacts"].items():
        if name != "run.json":
            try:
                validate_artifact_content(name, verified_bytes(engine.study, workspace, artifact))
            except StudyError as error:
                if error.code == "DELIVERABLE_ARTIFACT_INVALID":
                    state["failure"] = {"invalid_artifact": name, "message": localized(
                        task, f"Regenerate {name} as a valid file.", f"請重新產生有效的 {name} 檔案。")}
                raise
        artifacts[f"{unit['id']}__{name}"] = {**artifact, "filename": name}
    # The persisted status is used by existing checkpoints and dependencies.
    state["result"] = {"status": "verified", "validation_scope": PYTHON_VALIDATION_SCOPE, "run_id": result["run_id"],
                       "stdout": result["stdout"], "artifacts": artifacts, "source_hashes": input_hashes}
    return state["result"]


def merge_draft_edits(original, edited, regenerated):
    """Replay edits against unchanged lines and flag overlaps for review."""
    original_lines = original.splitlines(keepends=True)
    edited_lines = edited.splitlines(keepends=True)
    new_lines = regenerated.splitlines(keepends=True)
    unchanged = SequenceMatcher(None, original_lines, new_lines).get_matching_blocks()
    starts = [block.a for block in unchanged]
    edits = SequenceMatcher(None, original_lines, edited_lines).get_opcodes()
    preceding = []
    previous = None
    for index, line in enumerate(original_lines):
        preceding.append(previous)
        if line.strip():
            previous = index
    preceding.append(previous)

    changes = []
    conflicted = False
    for tag, start, end, edit_start, edit_end in edits:
        if tag == "equal":
            continue
        replacement = edited_lines[edit_start:edit_end]
        if tag == "insert":
            if start == len(original_lines) and not replacement[0].strip():
                # A separate addendum at the end belongs after the rebuilt report.
                changes.append((len(new_lines), len(new_lines), replacement))
                continue
            anchor = preceding[start]
            if anchor is None:
                changes.append((0, 0, replacement))
            else:
                index = bisect_right(starts, anchor) - 1
                if index < 0 or anchor >= unchanged[index].a + unchanged[index].size:
                    conflicted = True
                    continue
                old, new, count = unchanged[index]
                position = new + min(start - old, count)
                changes.append((position, position, replacement))
            continue
        kept = 0
        index = max(0, bisect_right(starts, start) - 1)
        while index < len(unchanged) and unchanged[index].a < end:
            old, new, count = unchanged[index]
            first, last = max(start, old), min(end, old + count)
            if first < last and (tag == "delete" or (first, last) == (start, end)):
                changes.append((new + first - old, new + last - old, replacement))
                kept += last - first
            index += 1
        conflicted |= kept != end - start
    for first, last, replacement in sorted(changes, key=lambda change: change[:2], reverse=True):
        new_lines[first:last] = replacement
    return "".join(new_lines), conflicted


def assemble_report(engine, user, task, stopped_for=None):
    workspace = engine.study.workspace(user, task["id"])
    sections, evidence_sections, sources, evidence_sources, coverage, actions, review = [], [], [], [], [], [], []
    units = task["plan"]["deliverables"]
    mandatory = {item["id"]: item["text"] for item in task["completion_inputs"]["requirements"]
                 if not optional_requirement(item)}
    requirement_scopes = mandatory_unit_requirements(units, mandatory.keys())
    mapped, actionable = set(), set()
    for unit in units:
        step_id = "unit-" + unit["id"]
        if stopped_for and step_id not in task["tool_results"]:
            result = mark_action([localized(
                task, f"The task reached its processing limit ({stopped_for}). Rerun or finish this deliverable manually.",
                f"任務已達處理上限（{stopped_for}）。請重新執行，或手動完成這項成果。",
            )])
        else:
            result = task["tool_results"][step_id]
        status = result["status"]
        reasons = result.get("reasons", [])
        refs = unit.get("requirement_ids", [])
        required_refs = [ref for ref in dict.fromkeys(refs) if ref in mandatory]
        optional = unit.get("optional", False) or bool(refs and not required_refs)
        action_refs = requirement_scopes.get(unit["id"], required_refs)
        unconfirmed = task.get("source_grounded_actions") and not optional and not action_refs
        item = {"deliverable_id": unit["id"], "title": unit["title"], "requirement": unit["objective"],
                "status": "execution_checked" if status == "verified" else status,
                "reasons": reasons, "source_ids": unit["source_ids"], "requirement_ids": refs,
                "source_refs": unit.get("source_refs", []), "source_locations": unit.get("source_locations", [])}
        if optional:
            item["optional"] = True
        else:
            mapped.update(required_refs)
        if unconfirmed:
            item["requirement_status"] = "unconfirmed"
        coverage.append(item)
        body = result.get("content", "")
        if recovery_notice := result.get("review_notice"):
            message = f"{unit['title']} — {recovery_notice}"
            item["review_notice"] = message
            if not optional or body:
                review.append(message)
        if status == "needs_action":
            if unconfirmed:
                message = localized(task, "This stage is incomplete. Check the assignment to confirm whether it is required.",
                                    "本階段尚未完成，請對照原題確認是否為必交成果。")
                item["status"] = "needs_review"
                item["review_notice"] = f"{unit['title']} — {' '.join([message, *dict.fromkeys(reasons)])}"
                review.append(item["review_notice"])
                actionable.add(unit["id"])
            elif not optional and result.get("blocked_by") and all(ref in actionable for ref in result["blocked_by"]):
                item["action_deferred"] = True
                actionable.add(unit["id"])
            elif not optional and reasons:
                title = unit["title"]
                if action_refs:
                    title += " (" + ", ".join(action_refs) + ")"
                    item["action_requirement_ids"] = action_refs
                item["action"] = f"{title} — {' '.join(dict.fromkeys(reasons))}"
                actions.append(item["action"])
                actionable.add(unit["id"])
        sources.extend(result.get("sources", []))
        if status == "verified":
            item["validation_scope"] = result.get("validation_scope", PYTHON_VALIDATION_SCOPE)
            recorded = ""
            if result["stdout"].strip():
                evidence = result["stdout"][:4000].replace("```", "` ` `")
                recorded = f"```text\n{evidence}\n```"
            images = []
            for name, artifact in result["artifacts"].items():
                data = verified_bytes(engine.study, workspace, artifact)
                if name.endswith(".png"):
                    images.append(f"![{artifact['filename']}]({name})")
                elif not recorded and artifact["filename"] != "run.json":
                    excerpt = data[:2000].decode("utf-8-sig", errors="replace").replace("```", "` ` `")
                    recorded = f"```text\n{excerpt}\n```"
            if recorded:
                label = localized(task, "The program produced the following output. Its values and method remain subject to review.",
                                  "程式產生以下輸出，數值與解法仍須核對。")
                evidence_sections.append({"heading": unit["title"], "content": f"{label}\n\n{recorded}"})
                evidence_sources.extend(unit["source_ids"])
            if images:
                sections.append({"heading": unit["title"], "content": "\n\n".join(images), "figure_only": True})
                sources.extend(unit["source_ids"])
        elif status == "user_supplied":
            body = localized(task, "Results supplied by the user", "使用者提供的結果")
            if result["notes"]:
                body += "\n\n" + "\n".join("> " + line for line in result["notes"].splitlines())
            for name, artifact in result["artifacts"].items():
                verified_bytes(engine.study, workspace, artifact)
                label = artifact["filename"]
                body += f"\n\n![{label}]({name})" if name.lower().endswith(".png") else f"\n\n- {label}"
        if body:
            sections.append({"heading": unit["title"], "content": body})
    for ref, requirement in mandatory.items():
        if ref not in mapped:
            actions.append(localized(task,
                f"Review the assignment requirement not mapped to a deliverable ({ref}): {requirement}",
                f"請核對尚未對應到成果的題目要求（{ref}）。{requirement}"))
    # Older checkpoints stored planner and unit messages together. Keep unresolved
    # source/tool evidence in review metadata while rebuilding current requirement actions.
    old_action_prefixes = tuple(f"{unit['title']} — " for unit in units) + (
        "Review the assignment requirement not mapped", "請核對尚未對應到成果的題目要求",
        "Could not summarize ", "無法統計「",
    )
    old_planner_blockers = set(task["completion_plan"].get("blockers", []))
    notes = dict.fromkeys(action for action in task.get("required_actions", [])
                          if not action.startswith(old_action_prefixes) and action not in old_planner_blockers)
    review.extend(notes)
    if not any(not section.get("figure_only") for section in sections) and evidence_sections:
        sections = evidence_sections + sections
        sources.extend(evidence_sources)
    for section in sections:
        section.pop("figure_only", None)
    if not sections:
        sections.append({"heading": localized(task, "Status", "處理狀態"), "content": localized(
            task, "No submission content is available yet.", "目前尚無可繳交的內容。")})
    actions = list(dict.fromkeys(actions))
    outcome = {"status": "needs_action" if actions or review else "complete", "blockers": [], "required_actions": actions}
    if review:
        outcome["review_required"] = review
    model = {"title": task["title"], "sections": sections, "sources": list(dict.fromkeys(sources)),
             "unresolved": [], "completion": outcome, "report_labels": {
                 "sources_heading": localized(task, "References", "來源"),
                 "actions_heading": localized(task, "Required user actions", "使用者待辦"),
             }}
    draft, model = engine.study.format_result(json.dumps(model, ensure_ascii=False), task)
    if snapshot := task.get("revision_draft"):
        saved = json.loads(verified_bytes(engine.study, workspace, snapshot, max_bytes=2_000_000))
        draft, conflicted = merge_draft_edits(saved["original"], saved["edited"], draft)
        task["revision"]["draft_edits_conflicted"] = conflicted
    task.update(draft=draft, report_model=model, completion=outcome, coverage=coverage, blockers=actions + review,
                skills=["assignment-reader", "report-writer", "assignment-completer"])
    engine.study.write_draft(user, task)
    return {"report": "report.md", "completion": outcome, "coverage": coverage}
