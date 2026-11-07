import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Header, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.agent.contracts import REGISTRY, AgentOptions
from backend.agent.engine import RESUMABLE, resume_unavailable
from backend.p0.ai import connection_status
from backend.services.exports import export_extra
from backend.services.study import StudyError, error_code


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200, repr=False)
    password: str = Field(min_length=1, max_length=300, repr=False)


class DeleteAccountRequest(BaseModel):
    confirmation: str = Field(min_length=64, max_length=64)
    confirmed: bool = False
    clear_ai: bool = False


class AssetRequest(BaseModel):
    selected_asset_ids: list[str] = Field(default_factory=list, max_length=20)
    approved: bool = False


class TaskRequest(BaseModel):
    assignment_id: str
    selected_asset_ids: list[str] = Field(default_factory=list, max_length=20)
    mode: str = "report_draft"
    instruction: str = Field(default="", max_length=4000)
    excerpt: str = Field(min_length=1, max_length=24000)
    requirements: str = Field(default="", max_length=6000)
    data_transfer_approved: bool = False
    allow_external_research: bool = False
    allow_code_execution: bool = False
    allow_visual_transfer: bool = False
    agent: AgentOptions = Field(default_factory=AgentOptions)


class ApproveRequest(BaseModel):
    request_sha256: str
    code_execution_approved: bool = False
    external_research_approved: bool = False


class DraftRequest(BaseModel):
    markdown: str = Field(min_length=1, max_length=80000)
    reviewed: bool = False
    accept_incomplete: bool = False


class CourseSelection(BaseModel):
    course_ids: list[str] = Field(min_length=1, max_length=30)


class MappingRequest(BaseModel):
    sis_id: str = Field(min_length=1, max_length=100)
    moodle_id: str = Field(min_length=1, max_length=30)


class DownloadApproval(BaseModel):
    approved: bool = False


class ExportRequest(BaseModel):
    kind: str = "docx"
    include_sources: bool = False


class CompletionRequest(BaseModel):
    approved: bool = False


class ProgressRequest(BaseModel):
    course_id: str | None = None
    assignment_id: str | None = None
    selected_asset_ids: list[str] = Field(default_factory=list, max_length=20)
    task_id: str | None = None
    page: str = "select"
    mode: str = "report_draft"
    instruction: str = Field(default="", max_length=4000)
    excerpt: str = Field(default="", max_length=24000)
    requirements: str = Field(default="", max_length=6000)
    editor: str = Field(default="", max_length=80000)


def success(value):
    return {"data": value, "meta": {"timestamp": datetime.now(UTC).isoformat()}, "error": None}


def routes(service):
    api = APIRouter()

    @api.get("/maintenance/deletion-preview")
    def deletion_preview():
        return success(service.maintenance.preview())

    @api.post("/maintenance/delete-account")
    def delete_account(body: DeleteAccountRequest):
        return success(service.maintenance.delete(**body.model_dump()))

    @api.get("/maintenance/diagnostics")
    def diagnostics():
        return JSONResponse(service.maintenance.diagnostics(), headers={
            "Content-Disposition": 'attachment; filename="diagnostics.json"'})

    @api.get("/dashboard")
    def dashboard():
        return success(service.learning.dashboard())

    @api.get("/learning/courses/{course_id}")
    def learning_course(course_id: str):
        return success(service.learning.course(course_id))

    @api.get("/search")
    def search(q: str = "", course_id: str | None = None,
               due_from: str | None = None, due_to: str | None = None):
        return success(service.learning.search(q, course_id, due_from, due_to))

    @api.post("/search/rebuild")
    def rebuild_search():
        return success(service.learning.rebuild_index())

    @api.get("/workflow/progress")
    def progress():
        return success(service.learning.progress())

    @api.put("/workflow/progress")
    def save_progress(body: ProgressRequest):
        return success(service.learning.save_progress(body.model_dump()))

    @api.post("/tasks/{task_id}/export-extra")
    def extra_export(task_id: str, body: ExportRequest):
        return success(export_extra(service, task_id, body.kind, body.include_sources))

    @api.get("/tools")
    def tools():
        return success({"registry": {name: {"approval_level": level, "input_schema": contract}
                                      for name, (level, contract) in REGISTRY.items()},
                        "sandbox": service.agent.runner.status()})

    @api.get("/connections")
    def connections():
        return success({"moodle": "cached" if service.user else "disconnected",
                        "sis": service.hub.sis.state if service.user and service.hub.sis_user == service.user else "disconnected",
                        "ntpu-ai": service.ai.rpc("login_status")})

    @api.post("/connections/moodle/start")
    def moodle_login(body: LoginRequest):
        return success(service.login(body.username, body.password))

    @api.post("/connections/moodle/restore")
    def moodle_restore():
        service.adopt_vault()
        return success(service.sync_courses())

    @api.post("/connections/sis/start")
    def sis_login(body: LoginRequest):
        return success(service.hub.connect_sis(body.username, body.password))

    @api.get("/hub")
    def hub():
        return success(service.hub.overview())

    @api.post("/sync/sis")
    def sis_sync():
        return success(service.hub.sync_sis())

    @api.post("/hub/courses/discover")
    def discover(semester: str | None = None):
        return success(service.hub.discover(semester))

    @api.post("/sync/moodle/full")
    def moodle_full(body: CourseSelection):
        return success(service.hub.sync_moodle(body.course_ids))

    @api.get("/hub/courses/{course_id}")
    def course_hub(course_id: str):
        return success(service.hub.course(course_id))

    @api.put("/hub/mappings")
    def mapping(body: MappingRequest):
        return success(service.hub.confirm_mapping(body.sis_id, body.moodle_id))

    @api.post("/materials/{asset_id}/download")
    def material_download(asset_id: str, body: DownloadApproval):
        if not body.approved:
            raise StudyError("DOWNLOAD_APPROVAL_REQUIRED")
        return success(service.hub.download_material(asset_id))

    @api.get("/materials/{asset_id}/file")
    def material_file(asset_id: str):
        asset = service.get("material", asset_id)
        if not asset.get("path"):
            raise StudyError("ASSET_NOT_DOWNLOADED")
        return FileResponse(service.safe_path(asset["path"]), filename=asset["name"],
                            media_type="application/octet-stream", content_disposition_type="attachment")

    @api.post("/connections/ntpu-ai/start")
    def ai_login():
        if not service.ai_lock.acquire(blocking=False):
            raise StudyError("AI_BUSY")
        try:
            return success(service.ai.rpc("open_login"))
        finally:
            service.ai_lock.release()

    @api.post("/connections/ntpu-ai/restore")
    def ai_restore():
        if not service.ai_lock.acquire(blocking=False):
            raise StudyError("AI_BUSY")
        try:
            return success(connection_status(service.ai))
        finally:
            service.ai_lock.release()

    @api.delete("/connections/{provider}")
    def logout(provider: str):
        service.logout(provider)
        return success({"disconnected": True})

    @api.get("/courses")
    def courses():
        return success(service.store.list(service.require_user(), "course"))

    @api.post("/sync/moodle")
    def sync(course_id: str | None = None):
        return success(service.sync_assignments(course_id) if course_id else service.sync_courses())

    @api.get("/courses/{course_id}/assignments")
    def assignments(course_id: str, include_removed: bool = False):
        service.get("course", course_id)
        return success(service.assignments(course_id, include_removed=include_removed))

    @api.get("/assignments/{assignment_id}")
    def assignment(assignment_id: str):
        value = service.get("assignment", assignment_id)
        assets = [service.get("asset", ref) for ref in value["asset_ids"]]
        return success({**value, "assets": assets})

    @api.post("/assignments/{assignment_id}/download")
    def download(assignment_id: str, body: AssetRequest):
        if not body.approved:
            raise StudyError("DOWNLOAD_APPROVAL_REQUIRED")
        return success(service.download(assignment_id, body.selected_asset_ids))

    @api.post("/assignments/{assignment_id}/parse")
    def parse(assignment_id: str, body: AssetRequest):
        return success(service.parse_assignment(assignment_id, body.selected_asset_ids))

    @api.post("/tasks", status_code=202)
    def create_task(body: TaskRequest, idempotency_key: str = Header()):
        return success(service.create_task(body.model_dump(), idempotency_key))

    @api.post("/assignments/{assignment_id}/complete", status_code=202)
    def complete_assignment(assignment_id: str, body: CompletionRequest, idempotency_key: str = Header()):
        if not body.approved:
            raise StudyError("COMPLETION_APPROVAL_REQUIRED")
        return success(service.complete_assignment(assignment_id, idempotency_key))

    @api.get("/tasks")
    def tasks():
        return success(service.store.list(service.require_user(), "task"))

    @api.get("/tasks/{task_id}")
    def task(task_id: str):
        value = service.get("task", task_id)
        if value["status"] in RESUMABLE:
            value = {**value, "resume_unavailable": resume_unavailable(value)}
        return success(value)

    @api.post("/tasks/{task_id}/approve", status_code=202)
    def approve(task_id: str, body: ApproveRequest):
        return success(service.approve(task_id, body.request_sha256, body.code_execution_approved,
                                       body.external_research_approved))

    @api.post("/tasks/{task_id}/resume", status_code=202)
    def resume(task_id: str, body: ApproveRequest):
        return success(service.agent.resume(task_id, body.request_sha256, body.code_execution_approved,
                                           body.external_research_approved))

    @api.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        return success(service.cancel(task_id))

    @api.put("/tasks/{task_id}/draft")
    def draft(task_id: str, body: DraftRequest):
        return success(service.save_draft(task_id, body.markdown, body.reviewed, body.accept_incomplete))

    @api.post("/tasks/{task_id}/export")
    def export(task_id: str):
        return success(service.export(task_id))

    @api.get("/tasks/{task_id}/events")
    async def events(task_id: str, request: Request):
        user = service.require_user()
        service.get("task", task_id)

        async def stream():
            cursor = 0
            for _ in range(900):
                if await request.is_disconnected():
                    return
                items = service.store.events(user, task_id, cursor)
                for item in items:
                    cursor = item["seq"]
                    yield f"id: {cursor}\ndata: {json.dumps(item)}\n\n"
                current = service.get("task", task_id, user)
                if current["status"] not in {"running", "planning", "exporting"}:
                    return
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @api.get("/artifacts/{task_id}/download")
    def artifact(task_id: str, kind: str = "pdf"):
        import hashlib
        task = service.get("task", task_id)
        workspace = service.workspace(service.user, task_id)
        if kind == "pdf" and task.get("pdf"):
            path, mime = service.safe_path(task["pdf"]), "application/pdf"
            if hashlib.sha256(path.read_bytes()).hexdigest() != task.get("qa", {}).get("sha256"):
                raise StudyError("ARTIFACT_CHANGED")
        elif kind == "markdown" and task["draft"]:
            path, mime = workspace / "work/report.md", "text/markdown"
        elif kind == "manifest" and task.get("pdf"):
            path, mime = workspace / "output/manifest.json", "application/json"
        elif kind in {"docx", "zip"} and kind in task.get("exports", {}):
            item = task["exports"][kind]
            path = service.safe_path(item["path"])
            if not path.resolve().is_relative_to(workspace.resolve()):
                raise StudyError("PATH_OUTSIDE_WORKSPACE")
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise StudyError("ARTIFACT_CHANGED")
            mime = ("application/zip" if kind == "zip" else
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        else:
            raise StudyError("ARTIFACT_NOT_AVAILABLE")
        if not path.resolve().is_relative_to(workspace.resolve()):
            raise StudyError("PATH_OUTSIDE_WORKSPACE")
        return FileResponse(path, filename=path.name, media_type=mime)

    @api.get("/tasks/{task_id}/artifacts/{name}")
    def tool_artifact(task_id: str, name: str):
        import hashlib
        task = service.get("task", task_id)
        # Names are registry outputs, never filesystem paths supplied by a client.
        results = [{"artifacts": task.get("generated_files", {})}]
        results.extend(reversed(list(task.get("tool_results", {}).values())))
        for result in results:
            artifact = result.get("artifacts", {}).get(name)
            if artifact:
                path = service.safe_path(artifact["path"])
                if not path.resolve().is_relative_to(service.workspace(service.user, task_id).resolve()):
                    raise StudyError("PATH_OUTSIDE_WORKSPACE")
                if hashlib.sha256(path.read_bytes()).hexdigest() != artifact["sha256"]:
                    raise StudyError("ARTIFACT_CHANGED")
                chart = name == "chart.png" and result.get("independently_verified") is True
                return FileResponse(path, filename=name,
                                    media_type="image/png" if chart else "application/octet-stream",
                                    content_disposition_type="inline" if chart else "attachment")
        raise StudyError("ARTIFACT_NOT_AVAILABLE")

    @api.get("/assets/{asset_id}/preview")
    def preview(asset_id: str):
        asset = service.get("asset", asset_id)
        if not asset.get("path"):
            raise StudyError("ASSET_NOT_DOWNLOADED")
        return FileResponse(service.safe_path(asset["path"]), filename=asset["name"],
                            media_type="application/octet-stream", content_disposition_type="attachment")

    return api


async def safe_exception(request, error):
    code = error_code(error)
    status = 404 if code == "NOT_FOUND_OR_NOT_AUTHORIZED" else 409 if "CONFLICT" in code or code == "AI_BUSY" else 400
    return JSONResponse({"data": None, "error": {"code": code, "message": code}}, status_code=status)
