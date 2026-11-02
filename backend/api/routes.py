import asyncio
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Header, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from backend.services.study import StudyError, error_code


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200, repr=False)
    password: str = Field(min_length=1, max_length=300, repr=False)


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


class ApproveRequest(BaseModel):
    request_sha256: str


class DraftRequest(BaseModel):
    markdown: str = Field(min_length=1, max_length=80000)
    reviewed: bool = False


def success(value):
    return {"data": value, "meta": {"timestamp": datetime.now(UTC).isoformat()}, "error": None}


def routes(service):
    api = APIRouter()

    @api.get("/connections")
    def connections():
        return success({"moodle": "cached" if service.user else "disconnected",
                        "ntpu-ai": service.ai.rpc("login_status")})

    @api.post("/connections/moodle/start")
    def moodle_login(body: LoginRequest):
        return success(service.login(body.username, body.password))

    @api.post("/connections/moodle/restore")
    def moodle_restore():
        service.adopt_vault()
        return success(service.sync_courses())

    @api.post("/connections/ntpu-ai/start")
    def ai_login():
        return success(service.ai.rpc("open_login"))

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
    def assignments(course_id: str):
        service.get("course", course_id)
        return success(service.assignments(course_id))

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

    @api.get("/tasks")
    def tasks():
        return success(service.store.list(service.require_user(), "task"))

    @api.get("/tasks/{task_id}")
    def task(task_id: str):
        return success(service.get("task", task_id))

    @api.post("/tasks/{task_id}/approve", status_code=202)
    def approve(task_id: str, body: ApproveRequest):
        return success(service.approve(task_id, body.request_sha256))

    @api.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        return success(service.cancel(task_id))

    @api.put("/tasks/{task_id}/draft")
    def draft(task_id: str, body: DraftRequest):
        return success(service.save_draft(task_id, body.markdown, body.reviewed))

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
        task = service.get("task", task_id)
        workspace = service.workspace(service.user, task_id)
        if kind == "pdf" and task.get("pdf"):
            path, mime = service.safe_path(task["pdf"]), "application/pdf"
        elif kind == "markdown" and task["draft"]:
            path, mime = workspace / "work/report.md", "text/markdown"
        elif kind == "manifest" and task.get("pdf"):
            path, mime = workspace / "output/manifest.json", "application/json"
        else:
            raise StudyError("ARTIFACT_NOT_AVAILABLE")
        return FileResponse(path, filename=path.name, media_type=mime)

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
