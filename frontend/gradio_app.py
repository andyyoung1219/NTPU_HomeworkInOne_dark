import time
import uuid
from datetime import datetime, timedelta, timezone

import gradio as gr

from backend.p0.ai import connection_status
from backend.services.exports import export_extra
from backend.services.study import WORKFLOW_MODES, error_code
from frontend.agent_tools import preview_markdown
from frontend.connections import ai_connection_message, build_connections
from frontend.data_hub import build_data_hub
from frontend.learning import build_learning, task_links
from frontend.navigation import CSS as CSS
from frontend.navigation import PAGES, SHELL_JS, page_updates, step_heading

STATUS = {"running": "AI 正在產生草稿", "waiting_approval": "等待傳送核准", "waiting_review": "草稿已完成，請審閱",
          "completed": "PDF 已匯出", "failed": "任務失敗，先前資料仍保留", "cancelled": "已取消",
          "blocked_auth": "需要確認 NTPU AI 登入", "partial": "已保留目前成果，請在此處續跑或重新產生"}

RECOVERABLE = {"partial", "cancelled", "blocked_auth", "failed"}
RECOVERY_ERRORS = {
    "TOOL_TIMEOUT": "等待工具或 AI 回覆逾時，不代表需要重新登入。",
    "AI_TIMEOUT": "NTPU AI 回覆逾時，不代表需要重新登入。",
    "OPERATION_TIMEOUT": "AI 連線操作逾時，請先檢查／恢復連線。",
    "AI_AUTH_REQUIRED": "請先檢查／恢復 AI 連線；確定需要登入時，再開啟登入視窗。",
    "AI_CONNECTION_PENDING": "AI 背景連線尚未就緒，請先檢查／恢復 AI 連線。",
    "TASK_DEADLINE_EXCEEDED": "原任務已超過總時限，請按「重新產生草稿」。",
    "COMPLETION_INPUT_REQUIRED": "先前任務因資料缺漏停止。請按「重新產生草稿」，套用先生成可用內容並列出使用者待辦的流程。",
}


def generation_status(task):
    label = STATUS.get(task["status"], task["status"])
    automatic = task.get("request", {}).get("mode") == "complete_assignment"
    if automatic and task["status"] == "running":
        label = "正在直接完成作業"
    completion = task.get("completion", {})
    required_actions = completion.get("required_actions", [])
    if automatic and task["status"] in {"waiting_review", "completed"} and (
            completion.get("status") == "needs_action" or required_actions):
        label = "已產生作業，請審閱並處理使用者待辦"
    if task.get("error"):
        code = task["error"]
        label += "\n" + RECOVERY_ERRORS.get(code, "請查看下方續跑說明。") + f"（{code}）"
    checkpoint, plan = task.get("checkpoint", {}), task.get("plan", {})
    if plan:
        label += f"\n已完成 {checkpoint.get('cursor', 0)}／{len(plan['steps'])} 個步驟。"
        cursor = checkpoint.get("cursor", 0)
        if automatic and task["status"] == "running" and cursor < len(plan["steps"]):
            label += "\n目前步驟：" + plan["steps"][cursor]["objective"]
    if automatic and checkpoint.get("consecutive_errors"):
        count = checkpoint["consecutive_errors"]
        stopped = count >= 3 or task["status"] != "running"
        label += f"\n連續失敗 {count}／3 次。" + ("已停止自動續跑。" if stopped else "系統將從中斷步驟自動續跑。")
    if automatic and task.get("completion", {}).get("blockers"):
        label += "\n尚待處理：" + "、".join(task["completion"]["blockers"])
    if automatic and required_actions:
        label += "\n使用者待辦：" + "、".join(required_actions)
    return label


def resume_reason(task):
    if task["status"] not in RECOVERABLE or not task.get("plan"):
        return "任務執行中或已有完整草稿，無須續跑。"
    if time.time() >= (task.get("deadline_at") or 0):
        return "原任務已超過總時限，請直接按「重新產生草稿」。"
    if task.get("error") in {"RETRY_BUDGET_EXCEEDED", "STEP_BUDGET_EXCEEDED",
                              "REQUEST_BUDGET_EXCEEDED", "PROMPT_BUDGET_EXCEEDED"}:
        return "原任務的執行次數或傳送預算已用完，請按「重新產生草稿」。"
    limits, checkpoint = task["plan"]["limits"], task["checkpoint"]
    cursor = checkpoint["cursor"]
    steps = task["plan"]["steps"]
    if cursor < len(steps):
        step = steps[cursor]
        if (checkpoint["attempts"].get(step["id"], 0) >= 1 + limits["max_retries"]
                or checkpoint["steps_used"] >= limits["max_steps"]
                or (step["tool"] in {"generate_report", "web_research"}
                    and checkpoint["requests_used"] >= limits["max_requests"])
                or checkpoint["prompt_chars_used"] >= limits["max_prompt_chars"]):
            return "原任務的執行次數或傳送預算已用完，請按「重新產生草稿」。"
    if task["request"]["mode"] != "complete_assignment" and (
            task["request"].get("allow_code_execution") or task["request"].get("allow_external_research")):
        return "此任務需要額外工具授權，請回原工具工作區核對權限後續跑。"
    return ""


def build_ui(service):
    def error(error):
        return "操作未完成：" + error_code(error)

    def course_options():
        courses = service.store.list(service.user, "course") if service.user else []
        return [(c["name"] + ("（快取）" if c.get("stale") else ""), c["id"]) for c in courses]

    def choose_courses():
        choices = course_options()
        return gr.update(choices=choices, value=choices[0][1] if choices else None)

    def assignment_options(course):
        if not service.user or not course:
            return gr.update(choices=[], value=None)
        items = service.assignments(course)
        choices = [(a["title"], a["id"]) for a in items]
        return gr.update(choices=choices, value=choices[0][1] if choices else None)

    def sync(course):
        try:
            service.sync_assignments(course)
            return assignment_options(course), "作業同步完成。"
        except Exception as exc:
            return assignment_options(course), error(exc) + "；仍可查看已保存的資料。"

    def detail(identifier):
        try:
            a = service.get("assignment", identifier)
            due = datetime.fromtimestamp(a["due_at"], tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if a.get("due_at") else "未提供截止日"
            info = f"{a['title']}\n截止：{due}（臺北時間）\n提交狀態：{a.get('submission_status', 'unknown')}\n最後同步：{a.get('last_synced_at', '未知')}"
            if a.get("stale"):
                info += "\n目前顯示上次成功同步的快取。"
            parts = a.get("description_parts", [])
            if parts:
                info += "\n文字來源：" + "、".join(part["label"] for part in parts)
            description_value = a["description"] or (
                "Moodle API 目前未回傳作業敘述文字。可使用下方附件，或到 Moodle 原頁核對；"
                "這不代表老師的網頁一定沒有文字。"
            )
            if a.get("description_lookup_issue"):
                description_value += "\n補查狀態：" + a["description_lookup_issue"]
            choices = []
            for ref in a["asset_ids"]:
                asset = service.get("asset", ref)
                choices.append((f"{asset['name']} · {asset.get('size_bytes', '?')} bytes · {asset['status']}", ref))
            return info, description_value, gr.update(choices=choices, value=[])
        except Exception as exc:
            return error(exc), "", gr.update(choices=[], value=[])

    def original_link(identifier):
        url = service.get("assignment", identifier).get("source_url")
        return f"[在 Moodle 查看作業原頁]({url})" if url else ""

    def prepared_values(result, message, identifier):
        req = "\n".join(f"[ ] {item['id']} {item['text']} (來源：{item['source']})" for item in result["requirements"])
        warnings = sorted({flag for asset in result["assets"] for flag in asset.get("flags", [])})
        if result["truncated"]:
            warnings.append("AI 節錄限 24000 字；請按需要刪改。")
        links = "\n".join(f"- [下載原附件：{asset['name']}](/api/assets/{asset['id']}/preview)" for asset in result["assets"])
        links = original_link(identifier) + "\n\n" + links
        return result["excerpt"], req, message + "\n" + "\n".join(warnings), links, False

    def prepare(assignment, selected):
        try:
            result = service.download(assignment, selected) if selected else service.parse_assignment(assignment, [])
            return prepared_values(result, "已讀取作業文字與選定附件。" if selected else "已讀取 Moodle 作業文字；本次不使用附件。", assignment)
        except Exception as exc:
            if error_code(exc) == "ASSIGNMENT_TEXT_NOT_AVAILABLE":
                return "", "", "Moodle API 未回傳作業文字。請選附件後讀取，或先到 Moodle 原頁核對。", original_link(assignment), False
            return "", "", error(exc), "", False

    def open_assignment(identifier):
        if not identifier:
            return "尚未選擇作業", "", gr.update(choices=[], value=[]), "", "", "", "", False
        return (*detail(identifier), *prepare(identifier, []))

    def start(assignment, assets, mode, instruction, excerpt, checklist, approved, visual_approved=False):
        if not approved:
            yield "", "請先核對節錄並核准本次傳送。", gr.update(), gr.update(), False
            return
        try:
            task = service.create_task({"assignment_id": assignment, "selected_asset_ids": assets,
                "mode": mode, "instruction": instruction, "excerpt": excerpt, "requirements": checklist,
                "data_transfer_approved": True, "allow_visual_transfer": visual_approved is True}, uuid.uuid4().hex)
            yield from watch_generation(task["id"])
        except Exception as exc:
            yield "", error(exc), gr.update(), gr.update(), False

    def start_completion(assignment):
        try:
            task = service.complete_assignment(assignment, uuid.uuid4().hex)
            yield from watch_generation(task["id"])
        except Exception as exc:
            yield "", error(exc), gr.update(), gr.update(), False

    def watch_generation(identifier):
        for _ in range(910):
            task = service.get("task", identifier)
            if task["status"] != "running":
                yield identifier, generation_status(task), task["draft"], preview_markdown(task), False
                return
            yield identifier, generation_status(task), gr.update(), gr.update(), False
            time.sleep(1)

    def recovery_controls(identifier):
        if not identifier:
            return "請先建立任務。", gr.update(interactive=False)
        task = service.get("task", identifier)
        reason = resume_reason(task)
        message = reason or "可從未完成的步驟續跑，已完成步驟會保留。續跑會沿用原任務資料，並核准再次傳送未完成的 AI 請求。"
        if task["status"] in RECOVERABLE:
            seconds = task["plan"]["limits"].get("ai_timeout", task["plan"]["limits"]["tool_timeout"]) if task.get("plan") else 60
            message += f"\n\n原任務 AI 等待上限：{seconds} 秒。重新產生的新任務預設等待上限為 180 秒；舊草稿保留在歷史任務。"
        return message, gr.update(interactive=not reason and not service.active_jobs)

    def refresh_generation(identifier):
        if not identifier:
            return "尚未建立任務。", *recovery_controls(identifier)
        return generation_status(service.get("task", identifier)), *recovery_controls(identifier)

    def check_generation_ai():
        if not service.ai_lock.acquire(blocking=False):
            return "目前任務仍在執行，請等待完成或先停止任務後再檢查連線。"
        try:
            return ai_connection_message(connection_status(service.ai))
        except Exception as exc:
            return error(exc) + "；連線尚未確認，請稍後重試。"
        finally:
            service.ai_lock.release()

    def login_generation_ai():
        if not service.ai_lock.acquire(blocking=False):
            return "目前任務仍在執行，請先停止任務，再開啟登入視窗。"
        try:
            service.ai.rpc("open_login")
            return "已開啟 NTPU AI 登入視窗。完成本人登入後，關閉視窗，再按「檢查／恢復 AI 連線」。"
        except Exception as exc:
            return error(exc)
        finally:
            service.ai_lock.release()

    def stop(task_id):
        try:
            service.cancel(task_id)
            return "已要求停止，保留現有草稿與紀錄。"
        except Exception as exc:
            return error(exc)

    def save(task_id, draft):
        try:
            service.save_draft(task_id, draft)
            return "草稿已保存；匯出前請重新確認審閱。", False
        except Exception as exc:
            return error(exc), False

    def export(task_id, draft, reviewed):
        if not reviewed:
            return "請先完成審閱並勾選確認。", ""
        try:
            service.save_draft(task_id, draft, reviewed=True, accept_incomplete=True)
            task = service.export(task_id)
            links = (f"### 匯出完成 · {task['qa']['pages']} 頁\n\n"
                     f"[下載 PDF](/api/artifacts/{task_id}/download?kind=pdf) · "
                     f"[下載 Markdown](/api/artifacts/{task_id}/download?kind=markdown) · "
                     f"[驗證與來源紀錄](/api/artifacts/{task_id}/download?kind=manifest)\n\n"
                     "PDF 已通過文字、空白頁、字型嵌入及溢出檢查，請開啟確認最後版面。")
            return "檔案匯出成功。", links + "\n\n" + files(task_id)
        except Exception as exc:
            return error(exc) + "；Markdown 草稿與已生成的程式檔仍可下載，也可另匯出 Word／ZIP。", files(task_id)

    def files(identifier):
        try:
            task = service.get("task", identifier)
            if not task["draft"]:
                return ""
            links = [task_links(task)]
            note = "程式檔由下方草稿中的程式區塊產生，修改後按「保存修改」更新檔案。\n\n" if task["request"]["mode"] == "code_solution" else ""
            if task["request"]["mode"] == "complete_assignment":
                completion = task.get("completion", {})
                if completion.get("status") == "needs_action" or completion.get("required_actions"):
                    note = "已產生作業，請審閱並處理報告末尾的使用者待辦。\n\n"
                    if completion.get("required_actions"):
                        note += "使用者待辦：" + "、".join(completion["required_actions"]) + "。\n\n"
                elif completion.get("blockers"):
                    note = "本次成果尚有待處理項目：" + "、".join(completion["blockers"]) + "。\n\n"
                elif completion.get("status") == "complete":
                    note = "本次自動執行已完成，請核對成果、來源與題目要求後匯出。\n\n"
            return note + " · ".join(links)
        except Exception:
            return ""

    def history():
        tasks = service.store.list(service.user, "task") if service.user else []
        return gr.update(choices=[(f"{t['title']} · {STATUS.get(t['status'], t['status'])} · {t['created_at'][:16]}", t["id"]) for t in tasks])

    def load_task(identifier):
        try:
            t = service.get("task", identifier)
            return t["id"], STATUS.get(t["status"], t["status"]), t["draft"], preview_markdown(t), False
        except Exception as exc:
            return "", error(exc), "", "", False

    def initial():
        course_update = choose_courses()
        course_id = course_update.get("value")
        assignment_update = assignment_options(course_id)
        assignment_id = assignment_update.get("value")
        if not assignment_id:
            return course_update, assignment_update, "", "", gr.update(choices=[], value=[]), "", "", "", ""
        info_value, description_value, asset_update = detail(assignment_id)
        selected = [ref for _, ref in asset_update.get("choices", [])
                    if service.get("asset", ref).get("status") == "parsed"]
        asset_update["value"] = selected
        excerpt_value, checklist_value, message, links = "", "", "", original_link(assignment_id)
        if selected or service.get("assignment", assignment_id).get("description", "").strip():
            result = service.parse_assignment(assignment_id, selected)
            excerpt_value = result["excerpt"]
            checklist_value = "\n".join(f"[ ] {item['id']} {item['text']} (來源：{item['source']})" for item in result["requirements"])
            message = "已恢復作業文字及先前選定的附件內容；AI 傳送仍需本次核准。"
            links += "\n\n" + "\n".join(f"- [下載原附件：{a['name']}](/api/assets/{a['id']}/preview)" for a in result["assets"])
        return course_update, assignment_update, info_value, description_value, asset_update, excerpt_value, checklist_value, message, links

    def latest_draft():
        tasks = service.store.list(service.user, "task") if service.user else []
        task = next((task for task in tasks if task.get("draft")), None)
        if not task:
            return "", "", "", "", False, ""
        links = ""
        if task.get("pdf"):
            links = (f"[下載已匯出的 PDF](/api/artifacts/{task['id']}/download?kind=pdf) · "
                     f"[下載 Markdown](/api/artifacts/{task['id']}/download?kind=markdown)")
        return task["id"], STATUS.get(task["status"], task["status"]), task["draft"], preview_markdown(task), False, links

    def edit_preview(markdown, identifier):
        try:
            task = service.get("task", identifier)
            return preview_markdown({**task, "draft": markdown}), False
        except Exception:
            return markdown, False

    def navigate(destination):
        return destination, *page_updates(destination)

    def require_assignment(identifier):
        if not identifier:
            raise gr.Error("請先選擇課程與作業，再繼續下一步。")
        try:
            service.get("assignment", identifier)
        except Exception as exc:
            raise gr.Error("無法讀取此作業，請重新選擇。") from exc

    def require_sources(text):
        if not text or not text.strip():
            raise gr.Error("請先讀取作業文字或選定附件，確認有內容後再繼續。")

    def validate_start(assignment, excerpt, approved):
        if not approved:
            raise gr.Error("請先核對節錄與要求，並勾選核准本次傳送。")
        if not assignment or not excerpt.strip():
            raise gr.Error("作業或節錄尚未準備好，請回上一步讀取資料。")

    def begin(assignment, assets, mode, instruction, excerpt, checklist, approved, visual_approved=False):
        yield from show_generation(start(assignment, assets, mode, instruction, excerpt, checklist, approved, visual_approved))

    def begin_completion(assignment):
        yield from show_generation(start_completion(assignment), auto_review=True)

    def poll_completion(identifier, current_page):
        if not identifier or current_page != "generate":
            return tuple(gr.update() for _ in generation_outputs)
        task = service.get("task", identifier)
        if task["request"]["mode"] != "complete_assignment":
            return tuple(gr.update() for _ in generation_outputs)
        if task["status"] == "running":
            updates = [gr.update() for _ in generation_outputs]
            updates[1] = generation_status(task)
            return tuple(updates)
        target = "review" if task["status"] in {"waiting_review", "completed"} and task.get("draft") else "generate"
        return (identifier, generation_status(task), task.get("draft", ""), preview_markdown(task), False,
                *navigate(target), gr.update(interactive=True), gr.update(interactive=True),
                gr.update(interactive=True), files(identifier), gr.update(), gr.update(interactive=True),
                *recovery_controls(identifier))

    def show_generation(stream, clear_draft=True, auto_review=False):
        # Navigation happens only after approval. Keep the previous draft until
        # a new task exists, so a rejected request cannot erase unsaved edits.
        first = True
        last_identifier = None
        for values in stream:
            identifier, status, draft, rendered, review = values
            if first and not identifier:
                gr.Warning(status)
                return
            last_identifier = identifier
            if first and clear_draft and isinstance(draft, dict):
                values = (identifier, status, "", "", False)
            first = False
            finished = not isinstance(draft, dict)
            yield (*values, *navigate("generate"), gr.update(interactive=False),
                   gr.update(interactive=finished), gr.update(interactive=finished),
                   "", "", gr.update(interactive=False), *recovery_controls(identifier))
        target = "generate"
        if auto_review and last_identifier:
            task = service.get("task", last_identifier)
            if task["status"] in {"waiting_review", "completed"} and task.get("draft"):
                target = "review"
        yield (gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               *navigate(target), gr.update(interactive=True),
               gr.update(interactive=True), gr.update(interactive=True), gr.update(), gr.update(),
               gr.update(interactive=True), *recovery_controls(identifier))

    def resume_generation(identifier):
        if not identifier:
            raise gr.Error("請先選擇要續跑的任務。")
        task = service.get("task", identifier)
        reason = resume_reason(task)
        if reason:
            raise gr.Error(reason)
        try:
            # Confirm readiness before spending another attempt on the failed AI
            # step. A login-window handoff can still be in progress.
            cursor = task["checkpoint"]["cursor"]
            steps = task["plan"]["steps"]
            if cursor < len(steps) and steps[cursor]["tool"] == "generate_report":
                if not service.ai_lock.acquire(blocking=False):
                    raise gr.Error("目前任務仍在執行，請先等待完成或停止任務。")
                try:
                    status = connection_status(service.ai)
                    if not status.get("ready"):
                        raise gr.Error(ai_connection_message(status))
                finally:
                    service.ai_lock.release()
            automatic = task["request"]["mode"] == "complete_assignment"
            service.agent.resume(identifier, task["request_sha256"], high=automatic, external=automatic)
        except gr.Error:
            raise
        except Exception as exc:
            raise gr.Error(error(exc) + "；請更新任務狀態，或重新產生草稿。") from exc
        yield from show_generation(watch_generation(identifier), clear_draft=False, auto_review=automatic)

    def regenerate(previous_id, assignment, assets, mode, instruction, excerpt, checklist, visual_approved=False):
        if not previous_id:
            raise gr.Error("請先從 AI 設定建立任務，再重新產生草稿。")
        with service.lock:
            previous = service.get("task", previous_id)
            if service.active_jobs or previous["status"] in {"running", "planning", "exporting"}:
                raise gr.Error("目前任務仍在執行，請等待完成，或停止後再重新產生。")
            if previous["assignment_id"] != assignment:
                raise gr.Error("作業已變更，請返回 AI 設定核對後再開始。")
        # Clicking the clearly labelled resend button approves this new request;
        # a fresh task ID keeps the previous draft and its artifacts in history.
        if previous["request"]["mode"] == "complete_assignment":
            yield from begin_completion(assignment)
            return
        validate_start(assignment, excerpt, True)
        yield from begin(assignment, assets, mode, instruction, excerpt, checklist, True, visual_approved)

    def review_task(identifier):
        if not identifier:
            raise gr.Error("請先產生草稿，或從歷史任務載入。")
        task = service.get("task", identifier)
        if not task.get("draft"):
            raise gr.Error("這次任務尚未產生草稿。請查看任務狀態，返回設定後重試。")

    def load_history(identifier):
        if not identifier:
            raise gr.Error("請先選擇要載入的歷史任務。")
        result = load_task(identifier)
        if not result[0]:
            raise gr.Error(result[1])
        return *result, *navigate("review"), "", ""

    def export_additional(identifier, draft, reviewed, kind, sources):
        if not reviewed:
            return "請先完成審閱並勾選確認。", ""
        try:
            task = service.get("task", identifier)
            if task["draft"] != draft or not task.get("reviewed"):
                service.save_draft(identifier, draft, reviewed=True, accept_incomplete=True)
            task = export_extra(service, identifier, kind, sources)
            message = "Word 已匯出並檢查檔案結構，請開啟核對字型與版面。"
            if kind == "zip":
                message = "ZIP 已匯出，包含 Markdown、程式檔及可用的文件。"
                omitted = task["exports"]["zip"].get("omitted_exports", {})
                if omitted:
                    message += " 未包含的格式：" + "、".join(f"{key.upper()}（{code}）" for key, code in omitted.items()) + "；詳見 ZIP 內 manifest.json。"
            return message, task_links(task)
        except Exception as exc:
            return error(exc), ""

    def workspace_values(value=None, destination=None):
        value = value or {}
        cid = value["course_id"] if "course_id" in value else choose_courses().get("value")
        assignment_update = assignment_options(cid)
        aid = value["assignment_id"] if "assignment_id" in value else assignment_update.get("value")
        assignment_update["value"] = aid
        course_update = gr.update(choices=course_options(), value=cid)
        if aid:
            info_value, desc, asset_update = detail(aid)
            selected = value.get("selected_asset_ids", [])
            asset_update["value"] = selected
            if "excerpt" in value:
                text, req, message, links = value["excerpt"], value.get("requirements", ""), "已恢復保存的輸入，請核對後重新核准傳送。", original_link(aid)
            else:
                text, req, message, links, _ = prepare(aid, [])
        else:
            info_value, desc, asset_update = "", "", gr.update(choices=[], value=[])
            text, req, message, links = "", "", "", ""
        identifier = value.get("task_id") or ""
        task = service.get("task", identifier) if identifier else None
        draft = value.get("editor", task.get("draft", "") if task else "")
        task_values = (identifier, generation_status(task) if task else "",
                       draft, preview_markdown({**task, "draft": draft}) if task else "", False)
        target = destination or value.get("page", "home")
        manual_mode = value.get("mode", "report_draft")
        if manual_mode not in WORKFLOW_MODES:
            manual_mode = "report_draft"
        return (course_update, assignment_update, info_value, desc, asset_update, text, req, message, links,
                manual_mode, value.get("instruction", "請依所選處理方式完成本次作業，缺漏資料與未驗證內容標記待補。"),
                *task_values, task_links(task) if task else "", files(identifier) if identifier else "", False, "",
                *navigate(target))

    def restore_workspace():
        value = service.learning.progress() if service.user else None
        return workspace_values(value)

    def open_from_dashboard(identifier):
        if not identifier:
            raise gr.Error("請先選擇一份作業。")
        assignment = service.get("assignment", identifier)
        return workspace_values({"course_id": assignment["course_id"], "assignment_id": identifier}, "sources")

    def open_saved_task(identifier):
        if not identifier:
            raise gr.Error("請先選擇歷史任務。")
        task = service.get("task", identifier)
        assignment = service.get("assignment", task["assignment_id"])
        value = {**task["request"], "course_id": assignment["course_id"], "task_id": identifier,
                 "editor": task.get("draft", "")}
        # Existing one-shot calls are never replayed by opening history.
        target = "generate" if task["status"] in RECOVERABLE | {"running"} else "review" if task.get("draft") else "configure"
        return workspace_values(value, target)

    def open_latest_task():
        tasks = service.store.list(service.require_user(), "task")
        task = next((t for t in tasks if t.get("draft")), None)
        if not task:
            raise gr.Error("尚無可繼續的草稿。")
        return open_saved_task(task["id"])

    def save_progress(owner, current_page, cid, aid, selected, mode, instruction, excerpt, requirements, identifier, draft):
        if not owner or owner != service.user:
            return "尚未登入，或帳號已切換。"
        if current_page not in {"select", "sources", "configure", "generate", "review"}:
            return gr.update()
        try:
            result = service.learning.save_progress({"page": current_page, "course_id": cid,
                "assignment_id": aid, "selected_asset_ids": selected, "mode": mode,
                "instruction": instruction, "excerpt": excerpt, "requirements": requirements,
                "task_id": identifier, "editor": draft})
            return "工作進度已保存 · " + result["saved_at"][:19]
        except Exception as exc:
            return "進度尚未保存：" + error_code(exc)

    with gr.Blocks(title="NTPU · 學習與作業中心", analytics_enabled=False) as ui:
        page = gr.State("home")
        with gr.Row(elem_id="app-bar"):
            gr.HTML('<div id="brand">NTPU <span style="color:#78d9b6">/</span> CAMPUS DESK</div>', scale=4)
            home_button = gr.Button("⌂ 回首頁", scale=1, min_width=100)
            connection_button = gr.Button("連線管理", scale=1, min_width=100)
        pages = {}
        with gr.Column(elem_id="page-home", elem_classes="app-page") as pages["home"]:
            gr.HTML('<div id="welcome" class="page-heading"><p class="eyebrow">YOUR PERSONAL CAMPUS SPACE</p>'
                    '<h1 tabindex="-1">今天，想先做什麼？</h1>'
                    '<p>掌握學習近況，或專心完成一份作業。<br>選擇你的工作空間，從這裡開始。</p></div>')
            with gr.Row(equal_height=True):
                with gr.Column(elem_classes="center-card", min_width=280):
                    gr.HTML('<div class="card-icon">▤</div><h2>學習資料中心</h2>'
                            '<p>你的學生資訊，一目了然。<br>查看課表、歷年成績、排名與畢業進度。</p>'
                            '<div class="card-meta">課表 / 成績 / 教材 / 公告</div>')
                    enter_hub = gr.Button("進入學習資料中心 →", variant="primary")
                with gr.Column(elem_classes="center-card", min_width=280):
                    gr.HTML('<div class="card-icon">✦</div><h2>懶人作業中心</h2>'
                            '<p>從題目到草稿，一步一步完成。<br>讀取作業、交給 AI，再由你審閱與匯出。</p>'
                            '<div class="card-meta">選作業 / AI 協作 / 審閱 / 匯出</div>')
                    enter_homework = gr.Button("進入懶人作業中心 →", variant="primary")
                    enter_completion = gr.Button("直接完成作業 →")
            learning = build_learning(service, ui)
            gr.HTML('<p class="home-note">個人資料保存在本機 · AI 傳送由你核准 · 非臺北大學官方產品</p>')
        with gr.Column(visible=False, elem_id="page-hub", elem_classes="app-page") as pages["hub"]:
            hub_refresh, hub_outputs = build_data_hub(service, ui)
        with gr.Column(visible=False, elem_id="page-select", elem_classes="app-page") as pages["select"]:
            gr.HTML(step_heading(0, "先選一份要處理的作業", "懶人作業中心 · 選擇課程，找到這次要完成的題目。"))
            run_style = gr.Radio([("逐步處理", "manual"), ("直接完成作業", "complete")],
                                 value="manual", label="作業流程")
            completion_scope = gr.Markdown(
                "選好作業後按「開始直接完成作業」，即核准系統讀取這份作業的附件、傳給 AI，"
                "並視需要查找公開資料及在隔離環境執行程式。系統會自動執行到審閱與匯出；"
                "逾時會從中斷步驟續跑；AI 連續失敗三次停止。非致命的資料或工具問題會先產出可用作業，"
                "在報告末尾列出使用者待辦。缺少 Docker 時會標示程式尚未執行；登入失效或完全無法生成才停止。"
                "最後由你審閱與匯出，不會自動繳交。",
                visible=False)
            with gr.Row():
                with gr.Column(scale=1, min_width=280, elem_classes="surface"):
                    course = gr.Dropdown(label="課程", choices=course_options())
                    refresh_courses = gr.Button("同步我的課程")
                    assignment = gr.Dropdown(label="作業", choices=[])
                    sync_button = gr.Button("同步此課程作業")
                    sync_status = gr.Textbox(label="同步狀態", interactive=False)
                with gr.Column(scale=2, elem_classes="surface"):
                    info = gr.Textbox(label="作業資訊與截止時間", lines=7, interactive=False)
                    gr.Markdown("找不到課程？請點右上角「連線管理」檢查 Moodle 登入狀態。")
            with gr.Accordion("繼續先前的草稿／歷史任務", open=False):
                history_button = gr.Button("更新歷史任務")
                historical = gr.Dropdown(label="保存的任務", choices=[])
                load_button = gr.Button("載入選定草稿 →")
                resume_button = gr.Button("繼續最近草稿 →")
            with gr.Row(elem_classes="page-actions"):
                select_home = gr.Button("← 回首頁")
                to_sources = gr.Button("下一步：讀取資料 →", variant="primary")
                complete_button = gr.Button("開始直接完成作業", variant="primary", visible=False)
        with gr.Column(visible=False, elem_id="page-sources", elem_classes="app-page") as pages["sources"]:
            gr.HTML(step_heading(1, "讀取題目與參考附件", "確認老師的作業說明，再選擇這次需要的資料。沒有附件也可以繼續。"))
            with gr.Row():
                with gr.Column(scale=2, elem_classes="surface"):
                    description = gr.Textbox(label="Moodle 作業敘述與活動指示", lines=12, interactive=False)
                with gr.Column(scale=1, min_width=280, elem_classes="surface"):
                    assets = gr.CheckboxGroup(label="本次選取的附件（PDF／DOCX／TXT／CSV）", choices=[])
                    download_button = gr.Button("讀取作業文字與選定附件", variant="primary")
                    attachment_links = gr.Markdown(sanitize_html=True)
                    download_status = gr.Textbox(label="解析狀態與限制", lines=4, interactive=False)
            with gr.Row(elem_classes="page-actions"):
                sources_back = gr.Button("← 上一步：選擇作業")
                to_configure = gr.Button("下一步：設定 AI →", variant="primary")
        with gr.Column(visible=False, elem_id="page-configure", elem_classes="app-page") as pages["configure"]:
            gr.HTML(step_heading(2, "這次，想請 AI 幫什麼忙？", "選擇處理方式，核對資料與要求，再核准本次傳送。"))
            mode = gr.Radio(choices=[(label, key) for key, label in WORKFLOW_MODES.items()], value="report_draft", label="處理方式")
            instruction = gr.Textbox(label="本次需求", value="請依所選處理方式完成本次作業，缺漏資料與未驗證內容標記待補。", lines=3)
            with gr.Row():
                checklist = gr.Textbox(label="題目要求檢查表（請核對、補充或更正）", lines=12)
                excerpt = gr.Textbox(label="本次將送給 NTPU AI 的節錄（可刪改）", lines=12)
            visual_approved = gr.Checkbox(label="允許將選定附件中的圖表／公式影像傳給 NTPU AI（最多四張頁面或圖片；與上方文字節錄分開核准）", value=False)
            approved = gr.Checkbox(label="已核對以上附件節錄與要求；核准本次傳給 NTPU AI")
            with gr.Row(elem_classes="page-actions"):
                configure_back = gr.Button("← 上一步：讀取資料")
                start_button = gr.Button("確認並開始產生草稿 →", variant="primary")
        with gr.Column(visible=False, elem_id="page-generate", elem_classes="app-page") as pages["generate"]:
            gr.HTML(step_heading(3, "交給 AI，準備你的草稿", "任務完成後，進入下一步審閱內容。你可以隨時停止目前任務。"))
            with gr.Column(elem_classes="surface"):
                task_status = gr.Textbox(label="任務狀態", lines=3, interactive=False)
                task_id = gr.Textbox(label="任務編號", interactive=False)
                stop_button = gr.Button("停止目前任務")
                with gr.Row():
                    check_ai_button = gr.Button("檢查／恢復 AI 連線")
                    login_ai_button = gr.Button("開啟 NTPU AI 登入視窗")
                    refresh_generation_button = gr.Button("更新任務狀態")
                generation_ai_status = gr.Textbox(label="AI 連線狀態", interactive=False,
                    value="先檢查／恢復 AI 連線；只有登入失效時才需要開啟登入視窗。")
                recovery_message = gr.Markdown(sanitize_html=True)
                resume_generation_button = gr.Button("從中斷處續跑（再次傳送未完成請求）", interactive=False)
                gr.Markdown("按「重新產生草稿」會將目前選定的資料與 AI 設定再次傳給 AI，建立新任務；先前草稿保留在歷史任務。需要修改內容時，請先返回 AI 設定。")
            with gr.Row(elem_classes="page-actions"):
                generate_back = gr.Button("← 返回 AI 設定")
                regenerate_button = gr.Button("重新產生草稿")
                to_review = gr.Button("下一步：審閱與匯出 →", variant="primary")
        with gr.Column(visible=False, elem_id="page-review", elem_classes="app-page") as pages["review"]:
            gr.HTML(step_heading(4, "最後，讓內容成為你的作品", "檢查草稿、程式與來源，補上待確認的內容，再匯出檔案。"))
            review_files = gr.Markdown(sanitize_html=True)
            with gr.Row():
                editor = gr.Textbox(label="草稿與程式編輯（Markdown，可修改程式區塊）", lines=24, scale=1)
                preview = gr.Markdown(label="草稿預覽", sanitize_html=True)
            save_button = gr.Button("保存修改")
            reviewed = gr.Checkbox(label="已審閱內容、程式、來源與待補事項，接受未完成項目並核准匯出")
            export_button = gr.Button("匯出 PDF 與原始檔", variant="primary")
            with gr.Row():
                export_kind = gr.Radio([("Word 文件", "docx"), ("完整成果 ZIP", "zip")], value="docx", label="其他匯出格式")
                include_sources = gr.Checkbox(label="ZIP 加入本次任務的題目與原始附件", value=False,
                    info="勾選後，將建立任務時保存的 Moodle 題目文字與本次選定附件放入 ZIP 的 sources 資料夾；不會重新下載或傳給 AI。")
            extra_export_button = gr.Button("匯出 Word／ZIP")
            export_status = gr.Textbox(label="匯出狀態", interactive=False)
            downloads = gr.Markdown(sanitize_html=True)
            with gr.Row(elem_classes="page-actions"):
                review_back = gr.Button("← 返回 AI 設定")
                next_assignment = gr.Button("選擇另一份作業 →")
        nav_outputs = [page, *(pages[name] for name in PAGES)]
        progress_status = gr.Textbox(label="本機工作進度", interactive=False)
        for button, destination in [(home_button, "home"), (select_home, "home"),
                                    (enter_hub, "hub"), (enter_homework, "select"),
                                    (sources_back, "select"), (configure_back, "sources"),
                                    (generate_back, "configure"), (review_back, "configure"),
                                    (next_assignment, "select")]:
            button.click(lambda dest=destination: navigate(dest), outputs=nav_outputs, api_name=False)
        enter_hub.click(hub_refresh, outputs=hub_outputs, api_name=False)
        enter_homework.click(lambda: "manual", outputs=run_style, api_name=False)
        enter_completion.click(lambda: (*navigate("select"), "complete"),
                               outputs=[*nav_outputs, run_style], api_name=False)

        def choose_run_style(style):
            automatic = style == "complete"
            return gr.update(visible=not automatic), gr.update(visible=automatic), gr.update(visible=automatic)

        run_style.change(choose_run_style, run_style,
                         [to_sources, complete_button, completion_scope], api_name=False)
        to_sources.click(require_assignment, assignment, api_name=False).success(
            lambda: navigate("sources"), outputs=nav_outputs, api_name=False)
        to_configure.click(require_sources, excerpt, api_name=False).success(
            lambda: navigate("configure"), outputs=nav_outputs, api_name=False)
        for button in [to_review]:
            button.click(review_task, task_id, api_name=False).success(
                lambda: navigate("review"), outputs=nav_outputs, api_name=False)

        def sync_courses():
            try:
                service.sync_courses()
                return "課程同步完成。", choose_courses()
            except Exception as exc:
                return error(exc) + "；請在連線管理檢查登入狀態。", choose_courses()

        refresh_course_event = refresh_courses.click(sync_courses, outputs=[sync_status, course], api_name=False)
        sync_event = sync_button.click(sync, course, [assignment, sync_status], api_name=False)
        asset_event = assets.input(lambda: (False, "", "", "附件選取已變更，請重新讀取作業文字與選定附件。"),
                                  outputs=[approved, excerpt, checklist, download_status], api_name=False)
        for component in [assets, assignment]:
            component.change(lambda: False, outputs=visual_approved, api_name=False)
        visual_approved.change(lambda: False, outputs=approved, api_name=False)
        for component in [excerpt, checklist, instruction, mode]:
            component.change(lambda: False, outputs=approved, api_name=False)
        task_outputs = [task_id, task_status, editor, preview, reviewed]
        generation_outputs = [*task_outputs, *nav_outputs, home_button, generate_back, to_review,
                              review_files, downloads, regenerate_button, recovery_message, resume_generation_button]
        completion_timer = gr.Timer(2)
        completion_timer.tick(poll_completion, [task_id, page], generation_outputs, queue=False, api_name=False)
        start_button.click(validate_start, [assignment, excerpt, approved], api_name=False).success(
                           begin, [assignment, assets, mode, instruction, excerpt, checklist, approved, visual_approved],
                           generation_outputs, api_name=False).then(files, task_id, review_files, api_name=False)
        complete_button.click(require_assignment, assignment, trigger_mode="once", api_name=False).success(
            begin_completion, assignment, generation_outputs, trigger_mode="once", api_name=False).then(
                files, task_id, review_files, api_name=False)
        regenerate_button.click(
            regenerate, [task_id, assignment, assets, mode, instruction, excerpt, checklist, visual_approved],
            generation_outputs, trigger_mode="once", api_name=False).then(files, task_id, review_files, api_name=False)
        resume_generation_button.click(resume_generation, task_id, generation_outputs,
            trigger_mode="once", api_name=False).then(files, task_id, review_files, api_name=False)
        check_ai_button.click(check_generation_ai, outputs=generation_ai_status, api_name=False)
        login_ai_button.click(login_generation_ai, outputs=generation_ai_status, api_name=False)
        refresh_generation_button.click(refresh_generation, task_id,
            [task_status, recovery_message, resume_generation_button], queue=False, api_name=False)
        task_id.change(recovery_controls, task_id, [recovery_message, resume_generation_button], api_name=False)

        def restore_run_style(identifier):
            if not identifier:
                return gr.update()
            task = service.get("task", identifier)
            return "complete" if task["request"]["mode"] == "complete_assignment" else "manual"

        task_id.change(restore_run_style, task_id, run_style, api_name=False)
        stop_button.click(stop, task_id, task_status, queue=False, api_name=False)
        editor.change(edit_preview, [editor, task_id], [preview, reviewed], api_name=False)
        save_button.click(save, [task_id, editor], [export_status, reviewed], api_name=False).then(files, task_id, review_files, api_name=False)
        export_button.click(export, [task_id, editor, reviewed], [export_status, downloads], api_name=False)
        extra_export_button.click(export_additional, [task_id, editor, reviewed, export_kind, include_sources],
                                  [export_status, downloads], api_name=False)
        history_button.click(history, outputs=historical, api_name=False)
        auth_events, check_events, auth_changed = build_connections(service, connection_button, page)
        displayed_user = gr.State(service.user)
        initial_outputs = [course, assignment, info, description, assets, excerpt, checklist, download_status, attachment_links]
        workspace_outputs = [*initial_outputs, mode, instruction, *task_outputs, downloads, review_files,
                             approved, export_status, *nav_outputs]
        selection_outputs = workspace_outputs[:-len(nav_outputs)]

        def switch_course(cid):
            return workspace_values({"course_id": cid})[:-len(nav_outputs)]

        def select_assignment(cid, identifier):
            if identifier and service.get("assignment", identifier)["course_id"] != cid:
                return tuple(gr.update() for _ in selection_outputs)
            return workspace_values({"course_id": cid, "assignment_id": identifier})[:-len(nav_outputs)]

        # Dropdown selection updates data only. Navigation belongs to the
        # explicit Next button, including when a delayed input arrives on blur.
        course_event = course.input(switch_course, course, selection_outputs, api_name=False)
        assignment_event = assignment.input(select_assignment, [course, assignment], selection_outputs, api_name=False)
        # Explicit sync updates the same form atomically; programmatic dropdown
        # changes must not erase the inputs being restored by another callback.
        sync_event.then(switch_course, course, selection_outputs, api_name=False)
        refresh_course_event.then(switch_course, course, selection_outputs, api_name=False)
        load_button.click(open_saved_task, historical, workspace_outputs, api_name=False)
        resume_button.click(open_latest_task, outputs=workspace_outputs, api_name=False)
        for button, component in learning["assignments"]:
            button.click(open_from_dashboard, component, workspace_outputs, api_name=False)
        for button, component in learning["tasks"]:
            button.click(open_saved_task, component, workspace_outputs, api_name=False)
        learning["restore"].click(restore_workspace, outputs=workspace_outputs, api_name=False)

        def open_search_result(index, hits):
            if index is None or int(index) >= len(hits):
                raise gr.Error("請選擇搜尋結果。")
            hit = hits[int(index)]
            if hit["kind"] == "task":
                values = open_saved_task(hit["ref"])
            elif hit.get("assignment_id"):
                values = open_from_dashboard(hit["assignment_id"])
            else:
                service.get("course", hit["course_id"])
                values = tuple(gr.update() for _ in workspace_outputs[:-len(nav_outputs)]) + navigate("home")
            return (*values, gr.update(value=hit["course_id"]), gr.update(selected="course"))

        learning["open_result"].click(open_search_result, [learning["selection"], learning["hits"]],
                                      [*workspace_outputs, learning["course"], learning["tabs"]], api_name=False)
        home_button.click(learning["refresh"], outputs=learning["outputs"], api_name=False)
        progress_inputs = [displayed_user, page, course, assignment, assets, mode, instruction,
                           excerpt, checklist, task_id, editor]
        for event in [course_event, assignment_event, asset_event]:
            event.then(save_progress, progress_inputs, progress_status, api_name=False)
        for component in [instruction, excerpt, checklist, mode, editor]:
            component.change(save_progress, progress_inputs, progress_status, trigger_mode="always_last", api_name=False)
        for component in [page, task_id]:
            component.change(save_progress, progress_inputs, progress_status, trigger_mode="always_last", api_name=False)
        download_button.click(prepare, [assignment, assets],
                              [excerpt, checklist, download_status, attachment_links, approved], api_name=False).then(
                                  save_progress, progress_inputs, progress_status, api_name=False)

        def refresh_identity(changed, known_user):
            if not changed or known_user == service.user:
                return (known_user, *(gr.update() for _ in workspace_outputs))
            return service.user, *restore_workspace()

        def clear_search(changed, known_user):
            if changed and known_user != service.user:
                return [], gr.update(choices=[], value=None), [], "", ""
            return tuple(gr.update() for _ in learning["search_clear"])

        for event in auth_events:
            event.then(clear_search, [auth_changed, displayed_user], learning["search_clear"], api_name=False).then(
                hub_refresh, outputs=hub_outputs, api_name=False).then(
                refresh_identity, [auth_changed, displayed_user],
                [displayed_user, *workspace_outputs], api_name=False).then(
                    learning["refresh"], outputs=learning["outputs"], api_name=False)
        for event in check_events:
            event.then(hub_refresh, outputs=hub_outputs, api_name=False).then(
                lambda known_user: refresh_identity(True, known_user), displayed_user,
                [displayed_user, *workspace_outputs], api_name=False)
        ui.load(restore_workspace, outputs=workspace_outputs, api_name=False)
        gr.HTML("", js_on_load=SHELL_JS)
    return ui
