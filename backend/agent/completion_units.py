"""Run and assemble independently checkable direct-completion deliverables."""

import ast
import hashlib
import io
import json
import re
import sys
import time
from pathlib import Path

from PIL import Image

from backend.agent.completion_context import unit_context
from backend.agent.contracts import REGISTRY, digest, fail, validate_step
from backend.agent.deliverables import unit_blockers
from backend.agent.runner import filesystem_path

RECOVERABLE_UNIT_ERRORS = {
    "AI_TIMEOUT", "AI_INVALID_REPORT_SCHEMA", "AI_INVALID_COMPLETION_PLAN", "TOOL_TIMEOUT",
    "AI_BRIDGE_ERROR", "AI_BRIDGE_EXITED", "AI_CONNECTION_PENDING",
    "OPERATION_TIMEOUT", "INCOMPLETE_MODEL_RESPONSE", "EMPTY_MODEL_RESPONSE",
    "PYTHON_EXECUTION_FAILED", "SANDBOX_NOT_AVAILABLE", "SANDBOX_EXECUTION_FAILED",
    "RUNNER_OUTPUT_LIMIT", "RUNNER_RESULT_INVALID", "DELIVERABLE_ARTIFACT_MISSING",
    "DELIVERABLE_ARTIFACT_INVALID", "AI_PROMPT_TOO_LARGE",
}
COMPLETION_LIMITS = {
    "TASK_DEADLINE_EXCEEDED", "STEP_BUDGET_EXCEEDED", "RETRY_BUDGET_EXCEEDED",
    "REQUEST_BUDGET_EXCEEDED", "PROMPT_BUDGET_EXCEEDED",
}
COMPACT_RETRY_ERRORS = {
    "AI_PROMPT_TOO_LARGE", "AI_TIMEOUT", "AI_BRIDGE_ERROR", "AI_BRIDGE_EXITED",
    "TOOL_TIMEOUT", "INCOMPLETE_MODEL_RESPONSE", "EMPTY_MODEL_RESPONSE", "AI_INVALID_REPORT_SCHEMA",
}


def expand_plan(task, units):
    """Insert only server-defined tools after the planning checkpoint."""
    if not units:
        return
    plan = task["plan"]
    steps = [
        validate_step({"id": "unit-" + unit["id"], "objective": unit["title"],
                       "tool": "complete_deliverable", "required_inputs": {"unit_id": unit["id"]},
                       "expected_artifacts": unit["expected_artifacts"],
                       "approval_level": REGISTRY["complete_deliverable"][0]})
        for unit in units
    ]
    index = next(i for i, step in enumerate(plan["steps"]) if step["tool"] == "execute_completion")
    plan["steps"][index:index + 1] = steps
    plan["deliverables"] = units
    task["plan_sha256"] = digest(plan)


def lookup(task, unit_id):
    return next(unit for unit in task["plan"]["deliverables"] if unit["id"] == unit_id)


def localized(task, en, zh):
    return en if task["completion_plan"].get("report_language") == "en" else zh


def mark_action(task, unit, reasons):
    from backend.agent.completion import add_actions

    add_actions(task, [f"{unit['title']} — {reason}" for reason in reasons])
    return {"status": "needs_action", "reasons": reasons}


def fallback_unit(task, unit_id, code):
    if code not in RECOVERABLE_UNIT_ERRORS:
        return None
    unit = lookup(task, unit_id)
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
    return {**mark_action(task, unit, [reason]), "degraded": True, "error_code": code}


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


def model_data(data, unit, compact):
    """Keep verified numeric summaries without repeating whole CSV previews."""
    selected = set(unit["source_ids"])
    keys = ("datasets", "attachment_profiles")
    relevant = selected & {entry["file_id"] for key in keys for entry in data.get(key, [])}
    inferred_profiles = {"csv-profiling", "public-uci-dataset"}.intersection(unit["required_capabilities"])
    result = {}
    for key in keys:
        entries = [entry for entry in data.get(key, []) if entry["file_id"] in relevant or
                   (not relevant and (not selected or inferred_profiles))]
        count = 2 if compact else 4
        result[key] = []
        for entry in entries[:count]:
            profile = entry["profile"]
            columns = profile["columns"]
            details = [column for column in columns if column and len(column) <= 120][:(8 if compact else 20)]
            if len(details) < len(columns):
                result["profiles_limited"] = True
            result[key].append({
                **{field: entry[field] for field in ("file_id", "title", "url", "sandbox_path") if field in entry},
                "profile": {"records": profile["records"], "column_count": len(columns), "columns": details,
                            "attributes": {column: {name: value for name, value in profile["attributes"][column].items()
                                                    if name != "frequencies" or (not compact and
                                                        len(json.dumps(value, ensure_ascii=False)) <= 250)}
                                           for column in details}},
            })
        if len(entries) > count:
            result["profiles_limited"] = True
    return result


def execute_unit(engine, user, task, unit_id):
    from backend.agent.completion import source_snapshot

    unit = lookup(task, unit_id)
    links = unit["dependencies"] + unit.get("optional_dependencies", [])
    dependencies = {ref: task["tool_results"].get("unit-" + ref, {}) for ref in links}
    blockers = unit_blockers(unit, task, runner_available=True)
    for ref in unit["dependencies"]:
        result = dependencies[ref]
        if result.get("status") not in {"verified", "ready_for_review"}:
            title = lookup(task, ref)["title"]
            blockers.append(localized(task, f"First finish the required deliverable {title}.",
                                      f"請先完成前置成果「{title}」。"))
    if (unit["kind"] == "writing" and "python-standard-library" in unit["required_capabilities"]
            and not any(result.get("status") == "verified" for result in dependencies.values())):
        blockers.append(localized(task, "Provide verified execution results for this section.",
                                  "請提供已驗證的程式執行結果，再完成這個章節。"))
    if blockers:
        return mark_action(task, unit, blockers)
    previous_errors = [audit["error_code"] for audit in task["tool_audit"]
                       if audit["step_id"] == "unit-" + unit_id and audit.get("error_code")]
    compact = bool(COMPACT_RETRY_ERRORS.intersection(previous_errors))
    context = unit_context(source_snapshot(engine, user, task), unit, compact=compact)
    if context["missing_locations"]:
        missing = [f"{ref['file_id']} / {ref.get('page', ref.get('section'))}" for ref in context["missing_locations"]]
        locations = ", ".join(missing)
        return mark_action(task, unit, [localized(
            task, f"The required source pages or sections could not be read ({locations}). Supply their contents to finish this deliverable.",
            f"指定的來源頁節無法讀取（{locations}）。請補上該頁節內容再完成此項成果。",
        )])
    if unit["kind"] == "python":
        if not task["approvals"].get("high"):
            fail("TOOL_APPROVAL_REQUIRED")
        if not engine.runner.status()["available"]:
            return mark_action(task, unit, unit_blockers(unit, task, runner_available=False))

    workspace = engine.study.workspace(user, task["id"])
    files, evidence = {}, []
    for ref, result in dependencies.items():
        if result.get("status") not in {"verified", "ready_for_review"}:
            evidence.append({"deliverable": ref, "status": "needs_action"})
            continue
        summary = {"deliverable": ref, "status": result["status"]}
        if result["status"] == "verified":
            summary.update(stdout=result["stdout"][:(500 if compact else 1200)], files=[])
            for name, artifact in result["artifacts"].items():
                if artifact["filename"] == "run.json":
                    continue
                data = verified_bytes(engine.study, workspace, artifact)
                suffix = Path(name).suffix
                key = artifact["sha256"] + suffix
                files[key] = data
                entry = {"name": name, "sandbox_path": "/input/" + key}
                if suffix in {".json", ".csv", ".txt"}:
                    entry["excerpt"] = data[:(140 if compact else 450)].decode("utf-8", errors="replace")
                summary["files"].append(entry)
        else:
            summary["draft_context"] = result["content"][:(400 if compact else 1200)]
        evidence.append(summary)

    payload = {"assignment": task["title"], "deliverable": unit,
               "inputs": context, "dependencies": evidence, "data": model_data(task["completion_data"], unit, compact),
               "compact_context": compact, "previous_error": previous_errors[-1] if previous_errors else None,
               "report_language": task["completion_plan"].get("report_language"),
               "allowed_sources": list(task["source_hashes"])}
    if unit["kind"] == "python":
        return execute_python(engine, user, task, unit, workspace, files, payload)
    return draft_section(engine, user, task, unit, payload)


def draft_section(engine, user, task, unit, payload):
    from backend.agent.completion import ask

    instruction = (
        '只撰寫指定 deliverable，回覆 {"content":"本節正文 Markdown","sources":["實際使用的 file_id"],'
        '"required_actions":[]}。依題目指定語言撰寫。引用限 allowed_sources，'
        "只依來源及已驗證的前置產物陳述測量結果，draft_context 僅供行文參考。"
        "選用的前置成果若仍待處理，只說明已知內容與缺少的結果。來源節錄不足以完成時，列明缺少的依據。"
        "指出缺漏及下一步，正文最多 6000 字。"
    )
    result = ask(engine, user, task, instruction, payload)
    content, sources, actions = (result.get(key) for key in ("content", "sources", "required_actions"))
    if (not isinstance(content, str) or not content.strip() or len(content) > 6000
            or not isinstance(sources, list) or not all(isinstance(ref, str) and ref in task["source_hashes"] for ref in sources)
            or not isinstance(actions, list) or len(actions) > 8
            or not all(isinstance(action, str) and action.strip() and len(action) <= 400 for action in actions)):
        fail("AI_INVALID_REPORT_SCHEMA")
    if re.search(r"\bTODO\b|\bTBD\b|待填", content, re.I):
        actions.append(localized(task, "Fill the missing content in this section.",
                                 "請補齊本節標示待填的內容。"))
    for ref in unit["source_ids"]:
        if ref not in sources:
            name = task["source_names"].get(ref, ref)
            actions.append(localized(task, f"Verify and cite the required source {name}.",
                                     f"請核對並引用指定來源「{name}」。"))
    status = mark_action(task, unit, actions) if actions else {"status": "ready_for_review"}
    return {**status, "content": content.strip(), "sources": list(dict.fromkeys(sources))}


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
            "只讀 input_files 與前置成果提供的 /input 檔案。執行所需檢查並在 /output 產生指定 "
            "expected_artifacts，stdout 列出精簡結果。不得安裝套件或連網。程式最多 12000 字。"
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
    state["result"] = {"status": "verified", "run_id": result["run_id"],
                       "stdout": result["stdout"], "artifacts": artifacts, "source_hashes": input_hashes}
    return state["result"]


def assemble_report(engine, user, task, stopped_for=None):
    workspace = engine.study.workspace(user, task["id"])
    sections, sources, coverage = [], [], []
    for unit in task["plan"]["deliverables"]:
        step_id = "unit-" + unit["id"]
        if stopped_for and step_id not in task["tool_results"]:
            result = mark_action(task, unit, [localized(
                task, f"The task reached its processing limit ({stopped_for}). Rerun or finish this deliverable manually.",
                f"任務已達處理上限（{stopped_for}）。請重新執行，或手動完成這項成果。",
            )])
        else:
            result = task["tool_results"][step_id]
        status = result["status"]
        coverage.append({"deliverable_id": unit["id"], "title": unit["title"], "requirement": unit["objective"],
                         "status": status, "reasons": result.get("reasons", []), "source_ids": unit["source_ids"],
                         "requirement_ids": unit.get("requirement_ids", []), "source_refs": unit.get("source_refs", []),
                         "source_locations": unit.get("source_locations", [])})
        body = result.get("content", "")
        sources.extend(result.get("sources", []))
        if status == "verified":
            body = localized(task, f"Verified execution: {result['run_id']}", f"執行紀錄 {result['run_id']}")
            if result["stdout"].strip():
                evidence = result["stdout"][:4000].replace("```", "` ` `")
                body += f"\n\n```text\n{evidence}\n```"
                if len(result["stdout"]) > 4000:
                    body += "\n\n" + localized(task, "See the execution receipt for the full output.",
                                                "完整輸出請見執行紀錄檔。")
            for name, artifact in result["artifacts"].items():
                verified_bytes(engine.study, workspace, artifact)
                if name.endswith(".png"):
                    body += f"\n\n![{name}]({name})"
                else:
                    body += f"\n\n- {name}"
            sources.extend(unit["source_ids"])
        if body:
            sections.append({"heading": unit["title"], "content": body})
    if not sections:
        sections.append({"heading": localized(task, "Status", "處理狀態"), "content": localized(
            task, "See the required actions for the items that need additional inputs or tools.",
            "需要額外資料或工具的項目列於使用者待辦。")})
    actions = list(dict.fromkeys(task.get("required_actions", [])))
    outcome = {"status": "needs_action" if actions else "complete", "blockers": [], "required_actions": actions}
    model = {"title": task["title"], "sections": sections, "sources": list(dict.fromkeys(sources)),
             "unresolved": [], "completion": outcome, "report_labels": {
                 "sources_heading": localized(task, "References", "來源"),
                 "actions_heading": localized(task, "Required user actions", "使用者待辦"),
             }}
    draft, model = engine.study.format_result(json.dumps(model, ensure_ascii=False), task)
    task.update(draft=draft, report_model=model, completion=outcome, coverage=coverage, blockers=actions,
                skills=["assignment-reader", "report-writer", "assignment-completer"])
    engine.study.write_draft(user, task)
    return {"report": "report.md", "completion": outcome, "coverage": coverage}
