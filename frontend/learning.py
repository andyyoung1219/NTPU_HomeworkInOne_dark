"""Dashboard, course pages and local source search."""

import html
from urllib.parse import quote

import gradio as gr

from backend.services.learning import due_label
from backend.services.study import error_code
from frontend.data_hub import snapshot_label

KINDS = {"course": "課程", "assignment": "作業", "asset": "作業附件", "material": "教材", "task": "AI 任務"}


def escaped(value):
    return html.escape(str(value or ""))


def task_links(task):
    identifier = quote(task["id"], safe="")
    links = []
    kinds = (["pdf"] if task.get("pdf") else []) + (["markdown"] if task.get("draft") else [])
    kinds += list(task.get("exports", {}))
    for kind in kinds:
        label = {"pdf": "PDF", "markdown": "Markdown", "docx": "Word", "zip": "ZIP"}.get(kind, kind)
        links.append(f'<a href="/api/artifacts/{identifier}/download?kind={kind}">{label}</a>')
    for name in task.get("generated_files", {}):
        links.append(f'<a href="/api/tasks/{identifier}/artifacts/{quote(name, safe="")}">{escaped(name)}</a>')
    return " · ".join(links)


def build_learning(service, ui):
    def refresh():
        if not service.user:
            return ("<p>請先在連線管理登入，或登入後查看本機快取。</p>", [], [],
                    gr.update(choices=[], value=None), gr.update(choices=[], value=None),
                    gr.update(choices=[], value=None), gr.update(choices=[("全部課程", "")], value=""))
        data = service.learning.dashboard()
        cards = [("七日內截止", len(data["upcoming"])), ("逾期未提交", len(data["overdue"])),
                 ("提交待確認", len(data["unknown"])), ("待審閱草稿", len(data["review"]))]
        content = '<div class="dashboard-stats">' + "".join(
            f'<div><span>{title}</span><strong>{count}</strong></div>' for title, count in cards) + '</div>'
        content += '<p class="home-note">臺北時間 ' + data["date"] + ' · ' + escaped(snapshot_label(data["timetable"])) + '</p>'
        recent = "".join(f'<li>{escaped(t["course_name"])} / {escaped(t["title"])} — {task_links(t) or "尚無產物"}</li>'
                         for t in data["recent"])
        content += "<h3>最近成果</h3><ul>" + (recent or "<li>尚無歷史任務</li>") + "</ul>"
        today = [[x.get("period") or "未知", x["name"], x.get("classroom") or "未知",
                  x.get("teacher") or "未知"] for x in data["today"]]
        rows = [[a["course_name"], a["title"], a["due_label"], a["submission_label"],
                 a.get("last_synced_at") or "未知", "快取待更新" if a.get("stale") else "已保存"]
                for a in data["assignments"]]
        assignments = [(a["course_name"] + " / " + a["title"], a["id"]) for a in data["assignments"]]
        tasks = [(t["course_name"] + " / " + t["title"], t["id"]) for t in data["recent"]]
        courses = [(c["name"] + " · " + (c.get("semester") or "學期未知"), c["id"]) for c in data["courses"]]
        return (content, today, rows, gr.update(choices=assignments, value=None),
                gr.update(choices=tasks, value=None), gr.update(choices=courses, value=None),
                gr.update(choices=[("全部課程", ""), *courses], value=""))

    def course_view(cid):
        if not cid:
            return "", [], [], [], [], "", gr.update(choices=[], value=None), gr.update(choices=[], value=None)
        try:
            data = service.learning.course(cid)
            snapshots = data["snapshots"]
            labels = {"assignments": "作業", "contents": "教材", "announcements": "公告", "grades": "Moodle 評分"}
            status = "\n\n".join(labels[k] + "：" + snapshot_label(s) for k, s in snapshots.items())
            materials = []
            for section in snapshots["contents"].get("data") or []:
                materials.append([section["name"], "章節", section.get("summary", "")])
                for module in section.get("modules", []):
                    materials.append([module["name"], module.get("type", ""), module.get("description", "")])
            posts = [[p["title"], p["message"]] for p in snapshots["announcements"].get("data") or []]
            grades = [[g["name"], g.get("grade") or "未提供", g.get("range") or "", "Moodle 評分"]
                      for g in snapshots["grades"].get("data") or []]
            rows = [[a["title"], a["due_label"], a["submission_label"], len(a.get("asset_ids", []))]
                    for a in data["assignments"]]
            links = []
            for kind, assets in (("materials", data["materials"]), ("assets", data["assets"])):
                for asset in assets:
                    name = escaped(asset["name"])
                    suffix = "file" if kind == "materials" else "preview"
                    link = (f'<a href="/api/{kind}/{quote(asset["id"], safe="")}/{suffix}">{name}</a>'
                            if asset.get("path") else name + "（尚未下載）")
                    links.append("<li>" + link + "</li>")
            links += [f'<li>{escaped(t["title"])} · {escaped(t["created_at"][:16])} · {task_links(t) or "尚無產物"}</li>'
                      for t in data["tasks"]]
            return (status, materials, posts, rows, grades, "<ul>" + "".join(links) + "</ul>",
                    gr.update(choices=[(a["title"], a["id"]) for a in data["assignments"]], value=None),
                    gr.update(choices=[(t["title"] + " · " + t["created_at"][:16], t["id"]) for t in data["tasks"]], value=None))
        except Exception as exc:
            raise gr.Error("課程未能讀取：" + error_code(exc)) from exc

    def search(query, cid, start, end):
        try:
            result = service.learning.search(query, cid or None, start.strip() or None, end.strip() or None)
            hits = result["results"]
            rows = [[KINDS[h["kind"]], h["title"], h.get("page") or h.get("section") or "—",
                     due_label(h.get("due_at")), h["snippet"]] for h in hits]
            labels = [(f'{i + 1}. {h["title"]} · {KINDS[h["kind"]]} · 頁／節 {h.get("page") or h.get("section") or "—"}', str(i))
                      for i, h in enumerate(hits)]
            note = f"找到 {len(hits)} 筆。" + ("僅顯示前 100 筆，請縮小範圍。" if result["truncated"] else "")
            issues = result["index"]["issues"]
            note += "\n索引更新：" + result["index"]["indexed_at"]
            if issues:
                note += "\n\n解析限制：\n" + "\n".join(f'{x["name"]}：{x["code"]}' for x in issues[:30])
            return rows, gr.update(choices=labels, value=None), hits, note, ""
        except Exception as exc:
            raise gr.Error("搜尋未完成：" + error_code(exc)) from exc

    def source(hit_index, hits):
        if hit_index is None:
            return ""
        h = hits[int(hit_index)]
        text = f'<h3>{escaped(h["title"])}</h3><p>來源頁／節：{h.get("page") or h.get("section") or "—"}</p>'
        text += "<p>" + escaped(h["snippet"]).replace("\n", "<br>") + "</p>"
        if h["kind"] in {"asset", "material"}:
            asset = service.get(h["kind"], h["ref"])
            if asset.get("path"):
                prefix, suffix = ("assets", "preview") if h["kind"] == "asset" else ("materials", "file")
                text += f'<a href="/api/{prefix}/{quote(h["ref"], safe="")}/{suffix}">下載原始文件核對</a>'
            else:
                text += "<p>尚未下載；可在作業流程或學習資料中心選擇下載。</p>"
        return text

    with gr.Tabs() as tabs:
        with gr.Tab("學習總覽", id="overview"):
            summary = gr.HTML()
            refresh_button = gr.Button("更新總覽（讀取本機資料）")
            with gr.Accordion("今天的課程", open=True):
                today = gr.Dataframe(headers=["節次", "課程", "教室", "教師"], interactive=False)
            assignments_table = gr.Dataframe(headers=["課程", "作業", "截止（臺北）", "提交狀態", "最後同步", "資料狀態"], interactive=False)
            with gr.Row():
                assignment = gr.Dropdown(label="從總覽開啟作業", choices=[])
                open_assignment = gr.Button("開啟作業 →")
            with gr.Row():
                task = gr.Dropdown(label="從總覽開啟歷史任務", choices=[])
                open_task = gr.Button("開啟歷史任務 →")
            restore = gr.Button("繼續上次工作 →")
        with gr.Tab("課程專頁", id="course"):
            course = gr.Dropdown(label="查看課程", choices=[])
            course_refresh = gr.Button("更新課程專頁")
            course_status = gr.Textbox(label="資料來源與同步時間", lines=3, interactive=False)
            course_assignments = gr.Dataframe(headers=["作業", "截止（臺北）", "提交狀態", "附件數"], interactive=False)
            with gr.Row():
                course_assignment = gr.Dropdown(label="本課作業", choices=[])
                open_course_assignment = gr.Button("處理本課作業 →")
            with gr.Accordion("教材與公告", open=False):
                materials = gr.Dataframe(headers=["名稱", "類型", "說明"], interactive=False)
                posts = gr.Dataframe(headers=["公告", "內容"], interactive=False)
                gr.Markdown("新教材請到下方「學習資料中心」同步並核准下載。")
            with gr.Accordion("Moodle 評分", open=False):
                grades = gr.Dataframe(headers=["項目", "成績", "範圍", "來源"], interactive=False)
                gr.Markdown("正式校務成績另列於學習資料中心；Moodle 評分不代表正式學期成績。")
            course_files = gr.HTML()
            with gr.Row():
                course_task = gr.Dropdown(label="本課歷史任務", choices=[])
                open_course_task = gr.Button("查看本課任務 →")
        with gr.Tab("搜尋", id="search"):
            gr.Markdown("搜尋本機課程、作業、教材與已下載文件內容。結果保留來源頁／節；CSV 搜尋僅涵蓋欄位與前五筆。")
            query = gr.Textbox(label="名稱或文件關鍵字", placeholder="例如：分類、報告格式、HW2")
            with gr.Row():
                search_course = gr.Dropdown(label="搜尋課程範圍", choices=[("全部課程", "")], value="")
                due_from = gr.Textbox(label="截止日起（YYYY-MM-DD）")
                due_to = gr.Textbox(label="截止日迄（YYYY-MM-DD）")
            search_button = gr.Button("搜尋並更新本機索引", variant="primary")
            search_status = gr.Textbox(label="搜尋結果與解析限制", lines=4, interactive=False)
            results = gr.Dataframe(headers=["類型", "名稱", "頁／節", "截止（臺北）", "內容節錄"], interactive=False)
            hits = gr.State([])
            selection = gr.Dropdown(label="選擇搜尋結果", choices=[])
            source_view = gr.HTML()
            open_result = gr.Button("開啟結果所屬作業／課程／任務 →")

    refresh_outputs = [summary, today, assignments_table, assignment, task, course, search_course]
    refresh_button.click(refresh, outputs=refresh_outputs, api_name=False)
    course_outputs = [course_status, materials, posts, course_assignments, grades, course_files, course_assignment, course_task]
    course.change(course_view, course, course_outputs, api_name=False)
    course_refresh.click(course_view, course, course_outputs, api_name=False)
    for event in [search_button.click, query.submit]:
        event(search, [query, search_course, due_from, due_to],
              [results, selection, hits, search_status, source_view], api_name=False)
    selection.change(source, [selection, hits], source_view, api_name=False)
    ui.load(refresh, outputs=refresh_outputs, api_name=False)
    return {"refresh": refresh, "outputs": refresh_outputs, "tabs": tabs,
            "assignments": [(open_assignment, assignment), (open_course_assignment, course_assignment)],
            "tasks": [(open_task, task), (open_course_task, course_task)], "restore": restore,
            "open_result": open_result, "selection": selection, "hits": hits, "course": course,
            "search_clear": [results, selection, hits, search_status, source_view]}
