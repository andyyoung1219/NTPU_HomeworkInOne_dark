import time
import uuid
from datetime import datetime, timedelta, timezone

import gradio as gr

from backend.services.study import MODES, error_code

CSS = """
.gradio-container {max-width:1180px!important;margin:auto!important}
#hero {padding:22px 0 14px;border-bottom:1px solid #25433a;margin-bottom:20px}
#hero h1 {font-size:30px;letter-spacing:-.6px}
#hero p {color:#a9c7b9}
.prose {line-height:1.8}
textarea {line-height:1.7!important}
footer {display:none!important}
"""

STATUS = {"running": "AI 正在產生草稿", "waiting_approval": "等待傳送核准", "waiting_review": "草稿已完成，請審閱",
          "completed": "PDF 已匯出", "failed": "任務失敗，先前資料仍保留", "cancelled": "已取消",
          "blocked_auth": "需要重新登入 NTPU AI", "partial": "保留部分草稿，需人工處理"}


def build_ui(service):
    def error(error):
        return "操作未完成：" + error_code(error)

    def course_options():
        courses = service.store.list(service.user, "course") if service.user else []
        return [(c["name"] + ("（快取）" if c.get("stale") else ""), c["id"]) for c in courses]

    def choose_courses():
        choices = course_options()
        selected = next((key for name, key in choices if "人工智慧代理人原理及實務" in name), None)
        return gr.update(choices=choices, value=selected or (choices[0][1] if choices else None))

    def restore():
        try:
            service.adopt_vault()
            service.sync_courses()
            return "Moodle 本人連線正常，課程已同步。", choose_courses()
        except Exception as exc:
            return error(exc), choose_courses()

    def login(username, password):
        try:
            service.login(username, password)
            return "Moodle 已連結，帳密欄位已清空。", "", "", choose_courses()
        except Exception as exc:
            return error(exc), "", "", choose_courses()

    def ai_action(method):
        try:
            result = service.ai.rpc(method)
            if method == "login_status" and not result.get("ready") and result.get("authentication") == "unknown":
                result = service.ai.rpc("connect")
            return "已開啟專用瀏覽器，請本人登入。" if method == "open_login" else "NTPU AI 已連線。" if result.get("ready") else "請本人登入或稍後再次檢查。"
        except Exception as exc:
            return error(exc)

    def logout(provider):
        try:
            service.logout(provider)
            return "已登出並清除該連線的本機憑證。"
        except Exception as exc:
            return error(exc)

    def assignment_options(course):
        if not service.user or not course:
            return gr.update(choices=[], value=None)
        items = service.assignments(course)
        choices = [(a["title"], a["id"]) for a in items]
        selected = next((key for name, key in choices if "指定作業1" in name.replace(" ", "")), None)
        return gr.update(choices=choices, value=selected or (choices[0][1] if choices else None))

    def sync(course):
        try:
            service.sync_assignments(course)
            return assignment_options(course), "作業同步完成。"
        except Exception as exc:
            return assignment_options(course), error(exc) + "；仍可查看已保存的資料。"

    def detail(identifier):
        try:
            a = service.get("assignment", identifier)
            due = datetime.fromtimestamp(a["due_at"], tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if a["due_at"] else "未提供截止日"
            info = f"{a['title']}\n截止：{due}（臺北時間）\n提交狀態：{a['submission_status']}\n最後同步：{a['last_synced_at']}"
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
                choices.append((f"{asset['name']} · {asset['size_bytes']} bytes · {asset['status']}", ref))
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
        return (*detail(identifier), *prepare(identifier, []))

    def start(assignment, assets, mode, instruction, excerpt, checklist, approved):
        if not approved:
            yield "", "請先核對節錄並核准本次傳送。", gr.update(), gr.update(), False
            return
        try:
            task = service.create_task({"assignment_id": assignment, "selected_asset_ids": assets,
                "mode": mode, "instruction": instruction, "excerpt": excerpt, "requirements": checklist,
                "data_transfer_approved": True}, uuid.uuid4().hex)
            for _ in range(400):
                task = service.get("task", task["id"])
                label = STATUS.get(task["status"], task["status"])
                if task.get("error"):
                    label += " · " + task["error"]
                if task["status"] != "running":
                    yield task["id"], label, task["draft"], task["draft"], False
                    return
                yield task["id"], label, gr.update(), gr.update(), False
                time.sleep(1)
        except Exception as exc:
            yield "", error(exc), gr.update(), gr.update(), False

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
            service.save_draft(task_id, draft, reviewed=True)
            task = service.export(task_id)
            links = (f"### 匯出完成 · {task['qa']['pages']} 頁\n\n"
                     f"[下載 PDF](/api/artifacts/{task_id}/download?kind=pdf) · "
                     f"[下載 Markdown](/api/artifacts/{task_id}/download?kind=markdown) · "
                     f"[驗證與來源紀錄](/api/artifacts/{task_id}/download?kind=manifest)\n\n"
                     "PDF 已通過文字、空白頁、字型嵌入及溢出檢查，請開啟確認最後版面。")
            return "PDF 匯出成功。", links
        except Exception as exc:
            return error(exc) + "；Markdown 草稿已保存。", f"[下載草稿](/api/artifacts/{task_id}/download?kind=markdown)"

    def history():
        tasks = service.store.list(service.user, "task") if service.user else []
        return gr.update(choices=[(f"{t['title']} · {STATUS.get(t['status'], t['status'])} · {t['created_at'][:16]}", t["id"]) for t in tasks])

    def load_task(identifier):
        try:
            t = service.get("task", identifier)
            return t["id"], STATUS.get(t["status"], t["status"]), t["draft"], t["draft"], False
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
        return task["id"], STATUS.get(task["status"], task["status"]), task["draft"], task["draft"], False, links

    with gr.Blocks(title="NTPU Homework · 我的作業", analytics_enabled=False) as ui:
        gr.Markdown("# NTPU Homework\n把作業資料整理成可核對的草稿。\n\n**本機優先 · P1 MVP · AI 協作後由你審閱**", elem_id="hero")
        with gr.Tab("我的作業"):
            with gr.Row():
                with gr.Column(scale=1, min_width=280):
                    course = gr.Dropdown(label="課程", choices=course_options())
                    restore_button = gr.Button("連結已有 Moodle 帳號並同步課程")
                    sync_button = gr.Button("同步此課程作業", variant="primary")
                    assignment = gr.Dropdown(label="作業", choices=[])
                    open_button = gr.Button("查看作業與附件")
                    info = gr.Textbox(label="截止日與來源", lines=6, interactive=False)
                with gr.Column(scale=2):
                    description = gr.Textbox(label="Moodle 作業敘述與活動指示", lines=10, interactive=False)
                    gr.Markdown("作業頁文字會一起讀取；沒有附件也可產生 AI 草稿。")
                    assets = gr.CheckboxGroup(label="本次選取的附件（PDF／DOCX／TXT／CSV）", choices=[])
                    download_button = gr.Button("讀取作業文字與選定附件", variant="primary")
                    attachment_links = gr.Markdown(sanitize_html=True)
                    download_status = gr.Textbox(label="解析狀態與限制", interactive=False)
            sync_status = gr.Textbox(label="同步狀態", interactive=False)
        with gr.Tab("AI 草稿工作區"):
            with gr.Row():
                with gr.Column():
                    mode = gr.Radio(choices=[(label, key) for key, label in MODES.items()], value="report_draft", label="協作模式")
                    instruction = gr.Textbox(label="本次需求", value="請依題目要求整理繁體中文報告草稿，未執行的實驗與缺漏資料標記 TODO。", lines=3)
                    checklist = gr.Textbox(label="題目要求檢查表（請核對、補充或更正）", lines=9)
                with gr.Column():
                    excerpt = gr.Textbox(label="本次將送給 NTPU AI 的節錄（可刪改）", lines=16)
            approved = gr.Checkbox(label="已核對以上附件節錄與要求；核准本次傳給 NTPU AI")
            start_button = gr.Button("產生草稿", variant="primary")
            task_id = gr.Textbox(label="任務編號", interactive=False)
            task_status = gr.Textbox(label="任務狀態", interactive=False)
            stop_button = gr.Button("停止目前任務")
        with gr.Tab("審閱與匯出"):
            with gr.Row():
                editor = gr.Textbox(label="Markdown 草稿編輯", lines=24, scale=1)
                preview = gr.Markdown(label="草稿預覽", sanitize_html=True)
            save_button = gr.Button("保存修改")
            reviewed = gr.Checkbox(label="已審閱內容、來源與 TODO；接受未完成項目並核准匯出此版 PDF")
            export_button = gr.Button("產生繁體中文 PDF", variant="primary")
            export_status = gr.Textbox(label="匯出狀態", interactive=False)
            downloads = gr.Markdown(sanitize_html=True)
            with gr.Accordion("歷史任務", open=False):
                history_button = gr.Button("更新歷史")
                historical = gr.Dropdown(label="保存的任務", choices=[])
                load_button = gr.Button("載入選定草稿")
        with gr.Tab("連線管理"):
            gr.Markdown("本人憑證只保存在電腦；成績與學號不會自動混入草稿。")
            username = gr.Textbox(label="Moodle 帳號", type="password")
            password = gr.Textbox(label="Moodle 密碼", type="password")
            login_button = gr.Button("登入 Moodle")
            logout_moodle = gr.Button("登出 Moodle")
            moodle_status = gr.Textbox(label="Moodle 狀態", interactive=False)
            ai_open = gr.Button("開啟 NTPU AI 本人登入")
            ai_check = gr.Button("檢查／恢復 NTPU AI 連線")
            ai_logout = gr.Button("登出 NTPU AI")
            ai_status = gr.Textbox(label="NTPU AI 狀態", interactive=False)
        restore_button.click(restore, outputs=[sync_status, course], api_name=False)
        course.change(assignment_options, course, assignment, api_name=False)
        sync_button.click(sync, course, [assignment, sync_status], api_name=False)
        detail_outputs = [info, description, assets, excerpt, checklist, download_status, attachment_links, approved]
        open_button.click(open_assignment, assignment, detail_outputs, api_name=False)
        assignment.change(open_assignment, assignment, detail_outputs, api_name=False)
        download_button.click(prepare, [assignment, assets], [excerpt, checklist, download_status, attachment_links, approved], api_name=False)
        assets.input(lambda: (False, "", "", "附件選取已變更，請重新讀取作業文字與選定附件。"),
                     outputs=[approved, excerpt, checklist, download_status], api_name=False)
        excerpt.input(lambda: False, outputs=approved, api_name=False)
        checklist.input(lambda: False, outputs=approved, api_name=False)
        start_button.click(start, [assignment, assets, mode, instruction, excerpt, checklist, approved], [task_id, task_status, editor, preview, reviewed], api_name=False)
        stop_button.click(stop, task_id, task_status, queue=False, api_name=False)
        editor.input(lambda value: (value, False), editor, [preview, reviewed], api_name=False)
        save_button.click(save, [task_id, editor], [export_status, reviewed], api_name=False)
        export_button.click(export, [task_id, editor, reviewed], [export_status, downloads], api_name=False)
        history_button.click(history, outputs=historical, api_name=False)
        load_button.click(load_task, historical, [task_id, task_status, editor, preview, reviewed], api_name=False)
        login_button.click(login, [username, password], [moodle_status, username, password, course], queue=False, api_name=False)
        logout_moodle.click(lambda: logout("moodle"), outputs=moodle_status, api_name=False)
        ai_open.click(lambda: ai_action("open_login"), outputs=ai_status, api_name=False)
        ai_check.click(lambda: ai_action("login_status"), outputs=ai_status, api_name=False)
        ai_logout.click(lambda: logout("ntpu-ai"), outputs=ai_status, api_name=False)
        ui.load(initial, outputs=[course, assignment, info, description, assets, excerpt, checklist, download_status, attachment_links], api_name=False)
        ui.load(latest_draft, outputs=[task_id, task_status, editor, preview, reviewed, downloads], api_name=False)
    return ui
