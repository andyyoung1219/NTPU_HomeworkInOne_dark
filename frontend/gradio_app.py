import time
import uuid
from datetime import datetime, timedelta, timezone

import gradio as gr

from backend.services.study import WORKFLOW_MODES, error_code
from frontend.agent_tools import preview_markdown
from frontend.connections import build_connections
from frontend.data_hub import build_data_hub
from frontend.navigation import CSS as CSS
from frontend.navigation import PAGES, SHELL_JS, page_updates, step_heading

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
        if not identifier:
            return "尚未選擇作業", "", gr.update(choices=[], value=[]), "", "", "", "", False
        return (*detail(identifier), *prepare(identifier, []))

    def start(assignment, assets, mode, instruction, excerpt, checklist, approved):
        if not approved:
            yield "", "請先核對節錄並核准本次傳送。", gr.update(), gr.update(), False
            return
        try:
            task = service.create_task({"assignment_id": assignment, "selected_asset_ids": assets,
                "mode": mode, "instruction": instruction, "excerpt": excerpt, "requirements": checklist,
                "data_transfer_approved": True}, uuid.uuid4().hex)
            for _ in range(910):
                task = service.get("task", task["id"])
                label = STATUS.get(task["status"], task["status"])
                if task.get("error"):
                    label += " · " + task["error"]
                if task["status"] != "running":
                    yield task["id"], label, task["draft"], preview_markdown(task), False
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
            service.save_draft(task_id, draft, reviewed=True, accept_incomplete=True)
            task = service.export(task_id)
            links = (f"### 匯出完成 · {task['qa']['pages']} 頁\n\n"
                     f"[下載 PDF](/api/artifacts/{task_id}/download?kind=pdf) · "
                     f"[下載 Markdown](/api/artifacts/{task_id}/download?kind=markdown) · "
                     f"[驗證與來源紀錄](/api/artifacts/{task_id}/download?kind=manifest)\n\n"
                     "PDF 已通過文字、空白頁、字型嵌入及溢出檢查，請開啟確認最後版面。")
            return "檔案匯出成功。", links + "\n\n" + files(task_id)
        except Exception as exc:
            return error(exc) + "；Markdown 草稿已保存。", f"[下載草稿](/api/artifacts/{task_id}/download?kind=markdown)"

    def files(identifier):
        try:
            task = service.get("task", identifier)
            if not task["draft"]:
                return ""
            links = [f"[下載 Markdown 草稿](/api/artifacts/{identifier}/download?kind=markdown)"]
            links.extend(f"[下載 {name}](/api/tasks/{identifier}/artifacts/{name})"
                         for name in task.get("generated_files", {}))
            note = "程式檔由下方草稿中的程式區塊產生，修改後按「保存修改」更新檔案。\n\n" if task["request"]["mode"] == "code_solution" else ""
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

    def begin(assignment, assets, mode, instruction, excerpt, checklist, approved):
        # Navigation happens only after approval. Keep the previous draft until
        # a new task exists, so a rejected request cannot erase unsaved edits.
        first = True
        for values in start(assignment, assets, mode, instruction, excerpt, checklist, approved):
            identifier, status, draft, rendered, review = values
            if first and not identifier:
                gr.Warning(status)
                return
            first = False
            finished = not isinstance(draft, dict)
            yield (*values, *navigate("generate"), gr.update(interactive=False),
                   gr.update(interactive=finished), gr.update(interactive=finished),
                   "", "")
        yield (gr.update(), gr.update(), gr.update(), gr.update(), gr.update(),
               *navigate("generate"), gr.update(interactive=True),
               gr.update(interactive=True), gr.update(interactive=True), gr.update(), gr.update())

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
            gr.HTML('<p class="home-note">個人資料保存在本機 · AI 傳送由你核准 · 非臺北大學官方產品</p>')
        with gr.Column(visible=False, elem_id="page-hub", elem_classes="app-page") as pages["hub"]:
            hub_refresh, hub_outputs = build_data_hub(service, ui)
        with gr.Column(visible=False, elem_id="page-select", elem_classes="app-page") as pages["select"]:
            gr.HTML(step_heading(0, "先選一份要處理的作業", "懶人作業中心 · 選擇課程，找到這次要完成的題目。"))
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
                gr.Markdown("若顯示需要登入，請開啟右上角「連線管理」，登入後返回設定重試。")
            with gr.Row(elem_classes="page-actions"):
                generate_back = gr.Button("← 返回 AI 設定")
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
            export_status = gr.Textbox(label="匯出狀態", interactive=False)
            downloads = gr.Markdown(sanitize_html=True)
            with gr.Row(elem_classes="page-actions"):
                review_back = gr.Button("← 返回 AI 設定")
                next_assignment = gr.Button("選擇另一份作業 →")
        nav_outputs = [page, *(pages[name] for name in PAGES)]
        for button, destination in [(home_button, "home"), (select_home, "home"),
                                    (enter_hub, "hub"), (enter_homework, "select"),
                                    (sources_back, "select"), (configure_back, "sources"),
                                    (generate_back, "configure"), (review_back, "configure"),
                                    (next_assignment, "select")]:
            button.click(lambda dest=destination: navigate(dest), outputs=nav_outputs, api_name=False)
        enter_hub.click(hub_refresh, outputs=hub_outputs, api_name=False)
        to_sources.click(require_assignment, assignment, api_name=False).success(
            lambda: navigate("sources"), outputs=nav_outputs, api_name=False)
        to_configure.click(require_sources, excerpt, api_name=False).success(
            lambda: navigate("configure"), outputs=nav_outputs, api_name=False)
        for button in [to_review, resume_button]:
            button.click(review_task, task_id, api_name=False).success(
                lambda: navigate("review"), outputs=nav_outputs, api_name=False)

        def sync_courses():
            try:
                service.sync_courses()
                return "課程同步完成。", choose_courses()
            except Exception as exc:
                return error(exc) + "；請在連線管理檢查登入狀態。", choose_courses()

        refresh_courses.click(sync_courses, outputs=[sync_status, course], api_name=False)
        course.input(sync, course, [assignment, sync_status], api_name=False)
        # Programmatic course refresh reads cached assignments, without silently
        # starting another network sync on every screen change or login.
        course.change(assignment_options, course, assignment, api_name=False)
        sync_button.click(sync, course, [assignment, sync_status], api_name=False)
        detail_outputs = [info, description, assets, excerpt, checklist, download_status, attachment_links, approved]
        assignment.change(open_assignment, assignment, detail_outputs, api_name=False)
        download_button.click(prepare, [assignment, assets], [excerpt, checklist, download_status, attachment_links, approved], api_name=False)
        assets.input(lambda: (False, "", "", "附件選取已變更，請重新讀取作業文字與選定附件。"),
                     outputs=[approved, excerpt, checklist, download_status], api_name=False)
        for component in [excerpt, checklist, instruction, mode]:
            component.input(lambda: False, outputs=approved, api_name=False)
        task_outputs = [task_id, task_status, editor, preview, reviewed]
        start_button.click(validate_start, [assignment, excerpt, approved], api_name=False).success(
                           begin, [assignment, assets, mode, instruction, excerpt, checklist, approved],
                           [*task_outputs, *nav_outputs, home_button, generate_back, to_review,
                            review_files, downloads], api_name=False).then(files, task_id, review_files, api_name=False)
        stop_button.click(stop, task_id, task_status, queue=False, api_name=False)
        editor.input(edit_preview, [editor, task_id], [preview, reviewed], api_name=False)
        save_button.click(save, [task_id, editor], [export_status, reviewed], api_name=False).then(files, task_id, review_files, api_name=False)
        export_button.click(export, [task_id, editor, reviewed], [export_status, downloads], api_name=False)
        history_button.click(history, outputs=historical, api_name=False)
        load_button.click(review_task, historical, api_name=False).success(
            load_history, historical, [*task_outputs, *nav_outputs, downloads, export_status], api_name=False).then(files, task_id, review_files, api_name=False)
        auth_events, check_events, auth_changed = build_connections(service, connection_button, page)
        displayed_user = gr.State(service.user)
        initial_outputs = [course, assignment, info, description, assets, excerpt, checklist, download_status, attachment_links]

        def refresh_identity(changed, known_user):
            if not changed or known_user == service.user:
                return (known_user, *(gr.update() for _ in range(17)))
            return service.user, *initial(), *latest_draft(), files(latest_draft()[0]), False

        for event in auth_events:
            event.then(hub_refresh, outputs=hub_outputs, api_name=False).then(
                refresh_identity, [auth_changed, displayed_user],
                [displayed_user, *initial_outputs, *task_outputs, downloads, review_files, approved], api_name=False)
        for event in check_events:
            event.then(hub_refresh, outputs=hub_outputs, api_name=False).then(
                lambda known_user: refresh_identity(True, known_user), displayed_user,
                [displayed_user, *initial_outputs, *task_outputs, downloads, review_files, approved], api_name=False)
        ui.load(initial, outputs=initial_outputs, api_name=False)
        ui.load(latest_draft, outputs=[*task_outputs, downloads], api_name=False).then(files, task_id, review_files, api_name=False)
        gr.HTML("", js_on_load=SHELL_JS)
    return ui
