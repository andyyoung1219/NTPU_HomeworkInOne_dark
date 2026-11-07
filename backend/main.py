import atexit
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.api.routes import routes, safe_exception
from backend.p0.moodle import probe
from backend.services.study import StudyError, StudyService, error_code

PORT = 8766
ORIGIN = f"http://127.0.0.1:{PORT}"
FRONTEND = Path(__file__).resolve().parent.parent / "frontend" / "static"


def create_app(service=None, key=None):
    service = service or StudyService()
    key = key or secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app):
        yield
        service.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.state.key = key
    app.add_exception_handler(StudyError, safe_exception)
    app.add_exception_handler(probe.ProbeError, safe_exception)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Never echo credentials in Pydantic validation errors.
        return JSONResponse({"error": {"code": "INVALID_REQUEST"}}, status_code=422)

    @app.middleware("http")
    async def protect(request: Request, call_next):
        if request.headers.get("host") != f"127.0.0.1:{PORT}":
            return JSONResponse({"error": "HOST_NOT_ALLOWED"}, status_code=403)
        if request.headers.get("origin") not in (None, ORIGIN):
            return JSONResponse({"error": "ORIGIN_NOT_ALLOWED"}, status_code=403)
        if request.url.path not in {"/health", f"/unlock/{key}"}:
            if not secrets.compare_digest(request.cookies.get("study_session", ""), key):
                return JSONResponse({"error": "LOCAL_AUTH_REQUIRED"}, status_code=401)
        try:
            response = await call_next(request)
        except Exception as error:
            return JSONResponse({"error": {"code": error_code(error)}}, status_code=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/health")
    def health():
        return {"ok": True, "version": "0.4.0", "phase": "P4"}

    @app.get("/unlock/{provided}")
    def unlock(provided: str):
        if not secrets.compare_digest(key, provided):
            return JSONResponse({"error": "INVALID_KEY"}, status_code=401)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie("study_session", key, httponly=True, samesite="strict")
        return response

    app.include_router(routes(service), prefix="/api")
    app.mount("/static", StaticFiles(directory=FRONTEND), name="static")

    @app.get("/")
    def index():
        return FileResponse(FRONTEND / "index.html", headers={
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        })

    return app


if __name__ == "__main__":
    import uvicorn

    service = StudyService()
    key = secrets.token_urlsafe(32)
    url = f"{ORIGIN}/unlock/{key}"
    (service.root / "launch-url.txt").write_text(url, encoding="utf-8")
    atexit.register(service.close)
    print(f"NTPU Homework: {url}", flush=True)
    uvicorn.run(create_app(service, key), host="127.0.0.1", port=PORT, access_log=False, log_level="critical")
