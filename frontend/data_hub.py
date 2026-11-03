"""P2 data views. School grades and materials never enter the AI workspace."""

from datetime import datetime, timedelta, timezone

import gradio as gr

from backend.services.study import error_code

ERRORS = {
    "MOODLE_LOGIN_REQUIRED": "請先在「連線管理」登入 Moodle，或在「我的作業」恢復 Moodle 連線。",
    "moodle_login_required": "Moodle 連線尚未建立，請先恢復或重新登入 Moodle。",
    "invalidtoken": "Moodle 連線已過期，請先重新登入 Moodle，再連結校務。",
    "ACCOUNT_CHANGED_RECONNECT": "目前 Moodle 帳號尚未重新驗證，請先恢復 Moodle 連線。",
    "SIS_BRIDGE_NOT_ALLOWED": "校務入口已登入，但校方回傳的資料主機或橋接格式尚未支援。",
    "SIS_BRIDGE_LOGIN_URL_FORMAT": "校務入口已登入，但橋接登入網址不是支援的 HTTPS 格式。",
    "SIS_BRIDGE_LOGIN_HOST": "校務入口已登入，但橋接登入主機不在已支援的校方主機清單。",
    "SIS_BRIDGE_LOGIN_ROUTE": "校務入口已登入，但橋接登入路徑或參數與已知格式不同。",
    "SIS_BRIDGE_REPORT_URL_FORMAT": "校務入口已登入，但報表網址不是支援的 HTTPS 格式。",
    "SIS_BRIDGE_REPORT_HOST": "校務入口已登入，但報表主機不在已支援的校方主機清單。",
    "SIS_BRIDGE_REPORT_ROUTE": "校務入口已登入，但回傳的報表路徑或查詢項目與要求不同。",
    "SIS_BRIDGE_HOST_MISMATCH": "校務入口已登入，但橋接登入與報表指向不同主機。",
    "SIS_BRIDGE_FIELDS_FORMAT": "校務入口已登入，但橋接憑證欄位的資料型態與已知格式不同。",
    "SIS_BRIDGE_FIELDS_MISSING": "校務入口已登入，但校方未回傳完整的橋接憑證欄位。",
    "SIS_BRIDGE_FIELDS_EXTRA": "校務入口已登入，但校方橋接表單包含尚未支援的額外欄位。",
    "SIS_BRIDGE_CREDENTIALS_EMPTY": "校務入口已登入，但校方回傳的橋接憑證為空值或非文字。",
    "SIS_REDIRECT_REQUIRES_REVIEW": "校方回傳了尚未支援的跳轉，已停止該次讀取。",
    "SIS_LOGIN_REQUIRED": "校務連線已失效，請重新登入。",
    "SIS_BROWSER_LOGIN_REQUIRED": "此登入流程需要在校方入口完成驗證，請開啟官方頁面。",
    "SIS_MOODLE_ACCOUNT_MISMATCH": "請使用與目前 Moodle 相同的本人學校帳號。",
    "SIS_ACCOUNT_CHANGED": "校務帳號與原連結不同，已停止同步並保留原快取。",
    "SIS_SCHEMA_CHANGED": "校方頁面格式無法辨識，已保留上次成功資料。",
    "SIS_REPORT_ENCODING_UNSUPPORTED": "無法解讀校方報表的文字編碼。",
    "SIS_GRADUATION_SUMMARY_FORMAT": "無法辨識畢業進度的學分摘要格式。",
    "SIS_GRADUATION_CATEGORY_FORMAT": "無法辨識畢業進度的課程分類版面。",
    "SIS_GRADUATION_METRIC_FORMAT": "無法辨識畢業進度的分類學分欄位。",
    "SIS_GRADUATION_SUBJECT_FORMAT": "無法辨識畢業進度的課程明細。",
    "SIS_REPORT_NOT_AVAILABLE": "校方尚未提供此項資料或此帳號無存取權。",
    "SIS_PERMISSION_DENIED": "校方拒絕存取，請到官方頁面確認權限。",
    "RATE_LIMITED": "服務要求暫停請求；已保留快取，請稍後重試。",
    "NETWORK_UNAVAILABLE": "目前無法連線，顯示上次成功的快取。",
    "MOODLE_PERMISSION_DENIED": "此 Moodle 帳號未開放這項查詢權限。",
    "MATERIAL_OFFICIAL_PAGE_REQUIRED": "此格式請透過下方官方課程連結取得。",
    "COURSE_SELECTION_REQUIRED": "請選擇 1 至 30 門課程。",
}
SIS_REPORT_NAMES = {"timetable": "課表", "grades": "成績", "rank": "排名", "graduation": "畢業進度"}


def error_label(code):
    return ERRORS.get(code, "未完成操作，請確認連線與選取範圍。") + f"（{code}）"


def error_message(exc):
    return error_label(error_code(exc))


def update_label(snapshot):
    error = snapshot["error"]
    messages = [error_label(error)] if error else []
    if snapshot["id"] == "sis:graduation":
        issues = (snapshot.get("data") or {}).get("summary_issues", [])
        if issues:
            messages.append("未讀取的學分摘要：" + "、".join(issues))
    return "｜".join(messages) if error else "｜".join(["已更新", *messages])


def snapshot_label(s):
    timestamp = s.get("last_synced_at")
    timestamp = datetime.fromisoformat(timestamp).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M（臺北）") if timestamp else "無"
    source = {"sis": "官方校務", "moodle": "Moodle"}.get(s.get("source"), "尚未同步")
    label = f"來源：{source}｜最後成功同步：{timestamp}"
    if s.get("stale"):
        label += "｜快取／待更新"
    if s["error"] or s["status"] == "partial" or not s.get("stale"):
        label += "｜" + update_label(s)
    return label


def report_rows(snapshot):
    rows = []
    for section in (snapshot.get("data") or {}).get("sections", []):
        title = section["title"]
        for field in section["fields"]:
            rows.append([title, field["label"], field["value"], field.get("detail") or ""])
        for subject in section["subjects"]:
            details = " / ".join(subject.get("details", []))
            if subject.get("credits"):
                details = f"學分：{subject['credits']}；類別：{subject.get('category', '')}"
            rows.append([title, subject["title"], subject.get("grade") or "", details])
        rows += [[title, "官方說明", note, ""] for note in section["notes"]]
    return rows


def build_data_hub(service, ui):
    hub = service.hub

    def choices():
        return [(f"{c.get('semester') or '學期未知'} · {c['name']}", c["id"])
                for c in service.store.list(service.user, "course") if c.get("scope") != "inactive"] if service.user else []

    def refresh():
        if not service.user:
            return ("請先連結 Moodle，再以相同本人帳號連結校務。", [], [], [], [], "", "", "", "",
                    [], gr.update(choices=[], value=None), gr.update(choices=[], value=None))
        value = hub.overview()
        snapshots = value["sis"]
        timetable = snapshots["timetable"]
        rows = []
        for course in (timetable.get("data") or {}).get("courses", []):
            for slot in course["meetings"] or [{"weekday": None, "period": None, "classroom": None}]:
                rows.append([course["semester"], course["code"], course["name"], course["teacher"] or "未知",
                             "一二三四五六日"[slot["weekday"] - 1] if slot["weekday"] else "未知",
                             str(slot["period"] or "未知"), slot["classroom"] or "未知", course["schedule_raw"]])
        maps = value["mappings"]
        states = {"connected": "已連結", "login_required": "請重新登入；可查看本人快取", "disconnected": "尚未連結", "permission_denied": "權限不足"}
        mapping_states = {"matched": "已比對", "confirmed": "已人工確認", "needs_confirmation": "需人工確認", "unmatched": "沒有對應課程"}
        return ("校務連線：" + states.get(value["sis_connection"], value["sis_connection"]), rows,
                report_rows(snapshots["grades"]), report_rows(snapshots["rank"]), report_rows(snapshots["graduation"]),
                *(snapshot_label(snapshots[k]) for k in ("timetable", "grades", "rank", "graduation")),
                [[m["sis_id"], m["name"], mapping_states[m["status"]], m["moodle_id"] or "待確認"] for m in maps],
                gr.update(choices=[(m["sis_id"] + " · " + m["name"], m["sis_id"]) for m in maps], value=None),
                gr.update(choices=choices(), value=None))

    def connect(username, password):
        try:
            result = hub.connect_sis(username, password)
            if result.get("error"):
                message = "校務入口登入成功，但課表尚未讀取。" + error_label(result["error"])
            else:
                message = "校務已連結；課表已讀取，可同步成績、排名與畢業進度。"
        except Exception as exc:
            message = error_message(exc)
        return message, "", ""

    def sync_sis():
        try:
            result = hub.sync_sis()
            if all(snapshot["status"] == "synced" for snapshot in result.values()):
                return "同步完成。"
            return "\n".join(f"{SIS_REPORT_NAMES[name]}：{update_label(snapshot)}"
                             for name, snapshot in result.items())
        except Exception as exc:
            return error_message(exc)

    def discover(semester):
        try:
            courses = hub.discover(semester.strip() or None)
            options = [(f"{c.get('semester') or '學期未知'} · {c['name']}", c["id"]) for c in courses]
            return gr.update(choices=options, value=[]), gr.update(choices=choices(), value=None), f"找到 {len(courses)} 門課，請選擇同步範圍。"
        except Exception as exc:
            return gr.update(), gr.update(), error_message(exc)

    def sync_moodle(selected):
        try:
            result = hub.sync_moodle(selected)
            lines = []
            labels = {"assignments": "作業", "contents": "教材", "announcements": "公告", "grades": "Moodle 評分"}
            for cid, sections in result.items():
                lines.append(service.get("course", cid)["name"] + "：" + "；".join(
                    labels[k] + ("完成" if not s["error"] else "未更新（" + s["error"] + "）") for k, s in sections.items()))
            return "\n".join(lines), gr.update(choices=choices())
        except Exception as exc:
            return error_message(exc), gr.update()

    def course_view(identifier):
        if not identifier:
            return "", [], [], [], gr.update(choices=[], value=None), "", gr.update(choices=[], value=None)
        try:
            data = hub.course(identifier)
            rows, posts, grades, files = [], [], [], []
            for section in data["contents"].get("data") or []:
                if section.get("summary"):
                    rows.append([section["name"], "章節說明", "", section["summary"], ""])
                for module in section["modules"]:
                    rows.append([section["name"], module["type"], module["name"], module["description"], module["url"] or ""])
                    for item in module["contents"]:
                        if item.get("id"):
                            files.append(item)
                        rows.append([section["name"], item.get("type", "檔案"), item.get("name", ""),
                                     item.get("text", "") or ("可下載" if item.get("download_supported") else "請至官方頁面查看"),
                                     item.get("url") or ""])
            for post in data["announcements"].get("data") or []:
                posts.append([post["title"], post["message"], str(post.get("modified_at") or "未知"), post["url"]])
                files += [a for a in post["attachments"] if a.get("id")]
            for item in data["grades"].get("data") or []:
                grades.append([item["name"], item["grade"], item["range"], item["percentage"], "Moodle 評分"])
            hidden = [a for a in service.assignments(identifier, include_hidden=True) if a.get("hidden")]
            return ("\n".join(k + "：" + snapshot_label(s) for k, s in data.items()), rows, posts, grades,
                    gr.update(choices=[(f"{a['name']} · {a.get('size_bytes') or '?'} bytes", a["id"]) for a in files], value=None),
                    f"[在 Moodle 查看課程與不支援的教材格式](https://lms3.ntpu.edu.tw/course/view.php?id={identifier})",
                    gr.update(choices=[(a["title"], a["id"]) for a in hidden], value=None))
        except Exception as exc:
            return error_message(exc), [], [], [], gr.update(choices=[], value=None), "", gr.update(choices=[], value=None)

    def download(ref, approved):
        if not approved:
            return "請先核准下載選定教材。", "", False
        try:
            asset = hub.download_material(ref)
            return "已下載並驗證 SHA-256，保存在本人本機資料區。", f"[下載教材原檔](/api/materials/{asset['id']}/file)", False
        except Exception as exc:
            return error_message(exc), "", False

    def mapping_candidates(identifier):
        value = next((m for m in hub.mappings() if m["sis_id"] == identifier), None)
        return gr.update(choices=[(c["name"], c["id"]) for c in value["candidates"]] if value else [], value=None)

    def confirm(sis_id, moodle_id):
        try:
            hub.confirm_mapping(sis_id, moodle_id)
            return "課程對應已確認；校務成績與 Moodle 評分仍各自保存來源。"
        except Exception as exc:
            return error_message(exc)

    def restore_hidden(identifier):
        try:
            service.hide_assignment(identifier, False)
            return "已恢復顯示作業。"
        except Exception as exc:
            return error_message(exc)

    with gr.Tab("學習資料中心"):
        gr.Markdown("## 校務與 Moodle\n資料保存在本機。校務成績、Moodle 評分分開顯示；不會自動傳給 AI。\n\n"
                    "[新版校務入口](https://my.ntpu.edu.tw/) · [Moodle](https://lms3.ntpu.edu.tw/)")
        with gr.Accordion("連結本人校務帳號", open=False):
            gr.Markdown("先連結 Moodle，再使用相同本人帳號登入校務。校務 Session 僅保留至本次關閉或登出。")
            sis_username = gr.Textbox(label="校務帳號", type="password")
            sis_password = gr.Textbox(label="校務密碼", type="password")
            connect_button = gr.Button("登入校務並讀取課表")
            disconnect_button = gr.Button("登出校務")
        connection = gr.Textbox(label="校務連線", interactive=False)
        with gr.Row():
            sis_sync = gr.Button("同步課表、歷年成績、排名與畢業進度", variant="primary")
            refresh_button = gr.Button("讀取本機快取")
        sis_status = gr.Textbox(label="操作結果", lines=4, interactive=False)
        with gr.Tab("正式課表"):
            timetable_status = gr.Textbox(label="來源與同步時間", interactive=False)
            timetable = gr.Dataframe(headers=["學期", "課號", "課名", "老師", "星期", "節次", "教室", "官方時段原文"], interactive=False)
        with gr.Tab("正式成績"):
            grades_status = gr.Textbox(label="來源與同步時間", interactive=False)
            grades_table = gr.Dataframe(headers=["學年學期", "科目／項目", "官方成績／數值", "學分／說明"], interactive=False)
        with gr.Tab("官方排名"):
            rank_status = gr.Textbox(label="來源與同步時間", interactive=False)
            rank_table = gr.Dataframe(headers=["學年學期", "項目", "官方數值", "說明"], interactive=False)
        with gr.Tab("官方畢業進度"):
            graduation_status = gr.Textbox(label="來源與同步時間", interactive=False)
            graduation_table = gr.Dataframe(headers=["分類", "項目", "官方數值／狀態", "說明"], interactive=False)
        gr.Markdown("### Moodle 教材與公告\n留白學期只列目前正在修讀的課程；指定學期時只保留有明確學期標記的課程。")
        semester = gr.Textbox(label="指定學期（選填，例如 115-1）", max_lines=1)
        discover_button = gr.Button("列出可同步課程")
        selected = gr.CheckboxGroup(label="本次完整同步的課程", choices=choices())
        full_sync = gr.Button("同步選定課程的作業、教材、公告與評分", variant="primary")
        sync_status = gr.Textbox(label="同步結果", lines=4, interactive=False)
        course = gr.Dropdown(label="查看已保存課程", choices=choices())
        course_status = gr.Textbox(label="各來源快取狀態", lines=5, interactive=False)
        with gr.Tab("章節與教材"):
            contents = gr.Dataframe(headers=["章節", "類型", "名稱", "內容／說明", "官方連結"], interactive=False)
        with gr.Tab("公告"):
            announcements = gr.Dataframe(headers=["標題", "內容", "來源更新時間戳", "官方連結"], interactive=False)
        with gr.Tab("Moodle 評分"):
            moodle_grades = gr.Dataframe(headers=["評分項目", "Moodle 分數", "範圍", "百分比", "來源"], interactive=False)
        original = gr.Markdown(sanitize_html=True)
        gr.Markdown("教材原檔可下載 PDF、Office Open XML、文字、資料集、ZIP、圖片與程式文字；不會執行程式或自動送給 AI。")
        material = gr.Dropdown(label="教材／公告附件", choices=[])
        approved = gr.Checkbox(label="核准下載以上選定教材到本機")
        material_download = gr.Button("下載選定教材")
        material_status = gr.Textbox(label="下載結果", interactive=False)
        material_link = gr.Markdown(sanitize_html=True)
        with gr.Accordion("恢復私人隱藏的作業", open=False):
            hidden = gr.Dropdown(label="此課程已隱藏的作業", choices=[])
            unhide = gr.Button("恢復顯示")
            visibility_status = gr.Textbox(label="私人顯示設定", interactive=False)
        with gr.Accordion("校務與 Moodle 課程對應", open=False):
            gr.Markdown("課號、學期、課名完全一致才自動對應。有歧義時需人工確認；不同學期禁止配對。")
            mapping_table = gr.Dataframe(headers=["校務課號與學期", "課名", "對應狀態", "Moodle 課程 ID"], interactive=False)
            sis_course = gr.Dropdown(label="校務課程", choices=[])
            moodle_course = gr.Dropdown(label="同學期候選 Moodle 課程", choices=[])
            confirm_button = gr.Button("確認對應")
            mapping_status = gr.Textbox(label="對應結果", interactive=False)

    outputs = [connection, timetable, grades_table, rank_table, graduation_table,
               timetable_status, grades_status, rank_status, graduation_status, mapping_table, sis_course, course]
    course_outputs = [course_status, contents, announcements, moodle_grades, material, original, hidden]
    connect_button.click(connect, [sis_username, sis_password], [sis_status, sis_username, sis_password], api_name=False).then(refresh, outputs=outputs, api_name=False)
    disconnect_button.click(hub.disconnect_sis, api_name=False).then(refresh, outputs=outputs, api_name=False)
    sis_sync.click(sync_sis, outputs=sis_status, api_name=False).then(refresh, outputs=outputs, api_name=False)
    refresh_button.click(refresh, outputs=outputs, api_name=False)
    discover_button.click(discover, semester, [selected, course, sync_status], api_name=False)
    full_sync.click(sync_moodle, selected, [sync_status, course], api_name=False).then(course_view, course, course_outputs, api_name=False)
    course.change(course_view, course, course_outputs, api_name=False)
    material.change(lambda: (False, ""), outputs=[approved, material_link], api_name=False)
    material_download.click(download, [material, approved], [material_status, material_link, approved], api_name=False)
    sis_course.change(mapping_candidates, sis_course, moodle_course, api_name=False)
    confirm_button.click(confirm, [sis_course, moodle_course], mapping_status, api_name=False).then(refresh, outputs=outputs, api_name=False)
    unhide.click(restore_hidden, hidden, visibility_status, api_name=False).then(course_view, course, course_outputs, api_name=False)
    ui.load(refresh, outputs=outputs, api_name=False)
    return refresh, outputs
