"""Reviewable P3 plan and explicit tool permissions in the existing workspace."""

import time
import uuid

import gradio as gr

from backend.services.study import error_code


def preview_markdown(task):
    markdown = task["draft"]
    if task.get("statistics_verified"):
        markdown = markdown.replace(
            "![已獨立重算的 CSV 統計圖](chart.png)",
            f"![已獨立重算的 CSV 統計圖](/api/tasks/{task['id']}/artifacts/chart.png)")
    return markdown


def build_agent_tools(service, assignment, assets, mode, instruction, excerpt, checklist,
                      task_id, task_status, editor, preview, reviewed):
    with gr.Accordion("P3 工具計畫、資料分析與續跑", open=True):
        gr.Markdown("先建立計畫，核對資料、程式與查詢，再核准執行。程式只在無網路 Docker 沙箱內執行。")
        gr.Markdown("本頁沿用「Agent 工作區」所選的作業、模式、需求與節錄；歷史任務可先從「審閱與匯出」載入。")
        task_status = gr.Textbox(label="工具任務進度", interactive=False)
        with gr.Row():
            materials = gr.CheckboxGroup(label="本次可傳送給 AI 的同課程教材（須已下載）", choices=[])
            refresh = gr.Button("列出本課已下載教材與所選 CSV")
        with gr.Row():
            allow_code = gr.Checkbox(label="本次計畫包含程式執行")
            allow_research = gr.Checkbox(label="本次計畫包含 Crossref 公開文獻查詢")
        query = gr.Textbox(label="將傳送給 Crossref 的確切查詢", max_lines=3)
        with gr.Row():
            csv_asset = gr.Dropdown(label="CSV 分析資料（限上方已選附件）", choices=[])
            column = gr.Textbox(label="CSV 數值欄位名稱")
        code = gr.Code(label="程式測試的 Python（CSV 模式由系統產生並列在計畫預覽）", language="python")
        gr.Markdown("程式可讀 `/input/<資料 SHA256>.<副檔名>`；將結果寫到 `/output/`。僅提供 Python 標準函式庫。")
        with gr.Row():
            retries = gr.Number(label="每步最多重試", value=1, minimum=0, maximum=2, precision=0)
            requests = gr.Number(label="外部請求總預算", value=3, minimum=1, maximum=5, precision=0)
            timeout = gr.Number(label="單次工具秒數", value=120, minimum=1, maximum=120, precision=0)
        with gr.Row():
            prepare = gr.Button("建立並預覽工具計畫")
            inspect = gr.Button("查看目前任務計畫／進度")
            sandbox = gr.Button("檢查沙箱環境")
        plan = gr.JSON(label="核准快照：步驟、資料、程式、查詢與預算")
        receipt = gr.State({})
        transfer = gr.Checkbox(label="已核對此計畫，核准將選定節錄與工具結果（含程式輸出）傳給 NTPU AI")
        high = gr.Checkbox(label="已審閱計畫中的 Python，額外核准此次沙箱執行")
        external = gr.Checkbox(label="核准將計畫中的確切查詢傳給 Crossref")
        with gr.Row():
            execute = gr.Button("核准並執行此計畫", variant="primary")
            resume = gr.Button("核准從檢查點續跑")
        evidence = gr.JSON(label="工具紀錄、需求覆蓋與阻塞事項")
        artifact_links = gr.Markdown(sanitize_html=True)

    def snapshot(task):
        return {"task_id": task["id"], "request_sha256": task["request_sha256"],
                "plan": task.get("plan"), "sources": task["source_names"],
                "source_hashes": task["source_hashes"], "approved_data": task["request"]}

    def record(task):
        return {"status": task["status"], "checkpoint": task.get("checkpoint"),
                "tool_audit": task.get("tool_audit"), "coverage": task.get("coverage"),
                "blockers": task.get("blockers"), "runs": task.get("runs", [])}

    def choices(identifier, selected):
        try:
            current = service.get("assignment", identifier)
            choices = [(m["name"], m["id"]) for m in service.store.list(service.require_user(), "material")
                       if m["course_id"] == current["course_id"] and m.get("path") and not m.get("stale")]
            csvs = [(service.get("asset", ref)["name"], ref) for ref in selected
                    if service.get("asset", ref)["name"].lower().endswith(".csv")]
            return gr.update(choices=choices, value=[]), gr.update(choices=csvs, value=csvs[0][1] if csvs else None)
        except Exception:
            return gr.update(choices=[], value=[]), gr.update(choices=[], value=None)

    def create(identifier, selected, task_mode, text, document, requirements, selected_materials,
               code_enabled, research_enabled, research_query, source, field, python, retry, budget, seconds):
        try:
            task = service.create_task({"assignment_id": identifier, "selected_asset_ids": selected,
                "mode": task_mode, "instruction": text, "excerpt": document, "requirements": requirements,
                "allow_code_execution": code_enabled, "allow_external_research": research_enabled,
                "agent": {"selected_material_ids": selected_materials, "research_query": research_query,
                          "csv_asset_id": source or "", "csv_column": field, "python_code": python or "",
                          "max_retries": int(retry), "max_requests": int(budget), "tool_timeout": int(seconds)}},
                uuid.uuid4().hex)
            return task["id"], "計畫已建立，請核對下方快照後核准。", snapshot(task), snapshot(task), False, False, False
        except Exception as exc:
            return gr.skip(), "操作未完成：" + error_code(exc), None, {}, False, False, False

    def inspect_task(identifier):
        try:
            task = service.get("task", identifier)
            names = {name for result in task.get("tool_results", {}).values()
                     for name in result.get("artifacts", {})}
            links = " · ".join(f"[下載 {name}](/api/tasks/{identifier}/artifacts/{name})" for name in sorted(names))
            return snapshot(task), snapshot(task), record(task), links, False, False, False
        except Exception as exc:
            return {"error": error_code(exc)}, {}, {}, "", False, False, False

    def run(identifier, receipt, approved, code_approved, research_approved, continuing=False):
        if not approved or receipt.get("task_id") != identifier:
            yield "請先查看本任務計畫，核對並勾選核准。", gr.skip(), gr.skip(), False, gr.skip()
            return
        try:
            action = service.agent.resume if continuing else service.approve
            action(identifier, receipt["request_sha256"], code_approved, research_approved)
            for _ in range(910):
                task = service.get("task", identifier)
                status = f"{task['status']} · 已完成 {task['checkpoint']['cursor']}/{len(task['plan']['steps'])} 步"
                if task.get("error"):
                    status += " · " + task["error"]
                yield status, task["draft"], preview_markdown(task), False, record(task)
                if task["status"] != "running":
                    return
                time.sleep(1)
        except Exception as exc:
            yield "操作未完成：" + error_code(exc), gr.skip(), gr.skip(), False, gr.skip()

    refresh.click(choices, [assignment, assets], [materials, csv_asset], api_name=False)
    prepare.click(create, [assignment, assets, mode, instruction, excerpt, checklist, materials,
        allow_code, allow_research, query, csv_asset, column, code, retries, requests, timeout],
        [task_id, task_status, plan, receipt, transfer, high, external], api_name=False)
    inspect.click(inspect_task, task_id, [plan, receipt, evidence, artifact_links, transfer, high, external], api_name=False)
    sandbox.click(lambda: service.agent.runner.status(), outputs=evidence, api_name=False)
    inputs = [task_id, receipt, transfer, high, external]
    outputs = [task_status, editor, preview, reviewed, evidence]
    execute.click(run, inputs, outputs, api_name=False)

    def continue_task(*args):
        yield from run(*args, continuing=True)

    resume.click(continue_task, inputs, outputs, api_name=False)
