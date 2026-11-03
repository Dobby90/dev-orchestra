"""Local REST adapter. Run with uvicorn --factory dev_orchestra.api.main:create_app."""

import hmac
import ipaddress
import os
import re
import sqlite3
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from dev_orchestra.api.security import COOKIE, SESSION_TTL_SECONDS, Security, digest, runtime_key
from dev_orchestra.application.agent_settings import AgentSettingsService
from dev_orchestra.application.agent_usage import usage_view
from dev_orchestra.application.backend import BackendService
from dev_orchestra.application.commands import Commands
from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.notifications import NotificationService
from dev_orchestra.application.results import Results
from dev_orchestra.application.service import RunService
from dev_orchestra.application.web_contracts import (
    AgentSettingsInput,
    AgentSettingsView,
    AgentUsageView,
    ApprovalInput,
    BranchPage,
    ChangeInput,
    ChangesView,
    CleanupInput,
    CodexModelPage,
    DashboardView,
    DeferInput,
    DiffView,
    DispositionInput,
    EventPage,
    LogPage,
    NotificationPage,
    NotificationRead,
    OperationView,
    PreviewView,
    ProjectCreate,
    ProjectPage,
    ProjectPatch,
    ProjectView,
    Proposal,
    Reason,
    ReportView,
    ResumeInput,
    ReviewResultView,
    RunCreate,
    RunDetail,
    RunPage,
    SessionCreate,
    SessionView,
    SpecContent,
    SpecView,
    StepPage,
    SubmissionView,
    TestResultView,
    ValidationCandidate,
    ValidationDetectInput,
    ValidationDetection,
)
from dev_orchestra.config import ROLES, load_config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.migrations import upgrade


def create_app(config=None, allowed_roots=None, port=8000, static_directory=None):
    config = config or load_config(
        Path(os.environ["DEVORCHESTRA_CONFIG"]) if "DEVORCHESTRA_CONFIG" in os.environ else None
    )
    new = not config.storage.database.exists()
    runs = RunService(config)
    if new:
        upgrade(runs.store.path)
    with runs.store.connection() as db:
        if db.execute("SELECT version FROM schema_version").fetchone()[0] != 5:
            raise PipelineError("run devorchestra database-upgrade before starting API")
    roots = (
        allowed_roots
        if allowed_roots is not None
        else [
            Path(p) for p in os.environ.get("DEVORCHESTRA_ALLOWED_ROOTS", "").split(os.pathsep) if p
        ]
    )
    key = runtime_key(runs.store.path.with_suffix(".web-key"))
    backend = BackendService(runs, roots, key)
    security = Security(runs.store, key)
    agent_settings = AgentSettingsService(config)
    notifications = NotificationService(runs.store)
    app = FastAPI(
        title="DevOrchestra",
        version="0.1.0",
        docs_url=None,
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.backend = backend
    app.state.security = security
    origins = {f"http://127.0.0.1:{port}", "http://127.0.0.1:5173"}

    def error(code, message, status):
        return JSONResponse(
            {"code": code, "message": message, "request_id": "req-" + uuid.uuid4().hex},
            status_code=status,
        )

    @app.exception_handler(ApplicationError)
    async def application_error(request, exc):
        if exc.code == "LOCAL_RATE_LIMIT":
            return error(exc.code, str(exc), 429)
        if exc.code == "VALIDATION_ERROR":
            return error(exc.code, str(exc), 422)
        status = (
            404
            if exc.code.endswith("NOT_FOUND")
            else 412
            if exc.code == "VERSION_CONFLICT"
            else 401
            if exc.code == "SESSION_REQUIRED"
            else 409
        )
        return error(exc.code, str(exc), status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return error("VALIDATION_ERROR", "invalid request fields", 422)

    @app.exception_handler(PipelineError)
    async def pipeline_error(request, exc):
        if str(exc) == "concurrent state update":
            return error("VERSION_CONFLICT", "run version changed", 412)
        if str(exc).startswith("unknown run:"):
            return error("RUN_NOT_FOUND", "run not found", 404)
        return error("WORKFLOW_ERROR", "operation rejected; inspect local workflow state", 409)

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request, exc):
        return error("DATABASE_UNAVAILABLE", "database operation unavailable", 503)

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request, exc):
        return error("CONFLICT", "resource already exists or has references", 409)

    @app.exception_handler(OSError)
    async def filesystem_error(request, exc):
        return error("FILESYSTEM_UNAVAILABLE", "filesystem validation failed", 422)

    @app.middleware("http")
    async def protection(request, call_next):
        path = request.scope["path"]
        try:
            if not request.client or not ipaddress.ip_address(request.client.host).is_loopback:
                return error("CLIENT_DENIED", "loopback client required", 403)
        except ValueError:
            return error("CLIENT_DENIED", "loopback client required", 403)
        if request.headers.get("host") != f"127.0.0.1:{port}":
            return error("HOST_DENIED", "loopback host required", 403)
        origin = request.headers.get("origin")
        if origin and origin not in origins:
            return error("ORIGIN_DENIED", "origin denied", 403)
        if not (path == "/api" or path.startswith("/api/")):
            if request.method not in {"GET", "HEAD"}:
                return error("METHOD_DENIED", "read-only GUI files", 405)
            response = await call_next(request)
            from dev_orchestra.api.static import CSP

            response.headers["Content-Security-Policy"] = CSP
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "no-referrer"
            return response
        if path != "/api/health":
            if not origin and request.headers.get("sec-fetch-site") != "same-origin":
                return error("ORIGIN_DENIED", "browser origin required", 403)
            if request.method == "OPTIONS":
                return Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Origin": origin or "",
                        "Access-Control-Allow-Credentials": "true",
                        "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type,X-CSRF-Token,If-Match,Idempotency-Key",
                    },
                )
            if path != "/api/session" or request.method != "POST":
                try:
                    await run_in_threadpool(security.authenticate, request.cookies.get(COOKIE))
                except ApplicationError:
                    return error("SESSION_REQUIRED", "session required", 401)
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    if not origin or not hmac.compare_digest(
                        request.headers.get("x-csrf-token", ""),
                        security.csrf(request.cookies[COOKIE]),
                    ):
                        return error("CSRF_INVALID", "CSRF verification failed", 403)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                if not origin:
                    return error("ORIGIN_DENIED", "origin required", 403)
                kind = request.headers.get("content-type", "").split(";")[0]
                if (
                    not (request.method == "DELETE" and not request.headers.get("content-type"))
                    and kind != "application/json"
                    and not (path == "/api/specs" and kind == "multipart/form-data")
                ):
                    return error("CONTENT_TYPE_DENIED", "JSON required", 415)
                count = 0
                parts = []
                async for chunk in request.stream():
                    count += len(chunk)
                    if count > 2 * 1048576:
                        return error("PAYLOAD_TOO_LARGE", "request exceeds limit", 413)
                    parts.append(chunk)
                request._body = b"".join(parts)
        response = await call_next(request)
        response.headers["Cache-Control"] = (
            "no-cache, no-transform"
            if response.headers.get("content-type", "").startswith("text/event-stream")
            else "no-store"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    def version(request, rid, prefix="run"):
        value = request.headers.get("if-match")
        if value is None:
            raise HTTPException(428, "If-Match required")
        match = re.fullmatch(r'"' + re.escape(prefix + ":" + rid) + r':v(\d+)"', value)
        if not match:
            raise HTTPException(412, "invalid version")
        return int(match[1])

    def check_run(request, rid):
        run = backend.registered_run(rid)
        if run["version"] != version(request, rid):
            raise ApplicationError("VERSION_CONFLICT", "run version changed")
        return run

    @app.get("/api/docs", include_in_schema=False)
    def api_document():
        # Local JSON specification; no CDN script executes with the session.
        return app.openapi()

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.post("/api/session", status_code=201, response_model=SessionView)
    def session(body: SessionCreate, response: Response):
        token, expires = security.exchange(body.bootstrap_code)
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            samesite="strict",
            path="/api",
            max_age=SESSION_TTL_SECONDS,
        )
        return {"authenticated": True, "expires_at": expires, "csrf_token": security.csrf(token)}

    @app.get("/api/session", response_model=SessionView)
    def session_info(request: Request):
        token = request.cookies[COOKIE]
        return {
            "authenticated": True,
            "expires_at": security.authenticate(token),
            "csrf_token": security.csrf(token),
        }

    @app.delete("/api/session", status_code=204)
    def logout(request: Request, response: Response):
        with runs.store.connection() as db:
            db.execute(
                "DELETE FROM web_sessions WHERE session_hash=?", (digest(request.cookies[COOKIE]),)
            )
        response.delete_cookie(COOKIE, path="/api")

    @app.get("/api/validation-profiles", response_model=list[ValidationCandidate])
    def validation_profiles():
        from dev_orchestra.application.validation import profiles

        return profiles()

    @app.post("/api/project-validation/detect", response_model=ValidationDetection)
    def detect_validation(body: ValidationDetectInput):
        return backend.projects.detect_validation(Path(body.repository_path), body.base_branch)

    @app.post("/api/projects", status_code=201, response_model=ProjectView)
    def project_create(body: ProjectCreate):
        return backend.register(body)

    @app.get("/api/settings/agents", response_model=AgentSettingsView)
    def get_agent_settings():
        return agent_settings.get()

    @app.post("/api/settings/agents/models", response_model=CodexModelPage)
    def codex_models():
        return {"items": agent_settings.models(refresh=True)}

    @app.patch("/api/settings/agents", response_model=AgentSettingsView)
    def save_agent_settings(body: AgentSettingsInput):
        explicit = any(
            body.settings.for_role(role).model or body.settings.for_role(role).reasoning_effort
            for role in ROLES
        )
        return agent_settings.save(
            body.settings, body.expected_version, agent_settings.models() if explicit else []
        )

    @app.get("/api/projects", response_model=ProjectPage)
    def projects(
        limit: int = Query(50, ge=1, le=100), cursor: str = "", include_archived: bool = False
    ):
        with runs.store.connection() as db:
            rows = db.execute(
                "SELECT id FROM projects WHERE id>? AND (? OR archived_at IS NULL) ORDER BY id LIMIT ?",
                (cursor, include_archived, limit + 1),
            ).fetchall()
        items = [backend.project(r["id"]) for r in rows[:limit]]
        return {
            "items": items,
            "has_more": len(rows) > limit,
            "next_cursor": items[-1]["id"] if len(rows) > limit else None,
        }

    @app.get("/api/projects/{pid}", response_model=ProjectView)
    def project(pid: str, response: Response):
        value = backend.project(pid)
        response.headers["ETag"] = f'"project:{pid}:v{value["version"]}"'
        return value

    @app.patch("/api/projects/{pid}", response_model=ProjectView)
    def patch_project(pid: str, body: ProjectPatch, request: Request):
        return backend.change_project(pid, version(request, pid, "project"), body)

    @app.delete("/api/projects/{pid}", response_model=ProjectView)
    def archive_project(pid: str, request: Request):
        return backend.change_project(pid, version(request, pid, "project"))

    @app.get("/api/projects/{pid}/branches", response_model=BranchPage)
    def branches(pid: str):
        from dev_orchestra.infrastructure.git import Git

        p = backend.project(pid)
        backend.projects.validate_repository(Path(p["repository_path"]), p["default_base_branch"])
        return {
            "items": Git(Path(p["repository_path"]))
            .call("for-each-ref", "--format=%(refname:short)", "refs/heads/")
            .splitlines()
        }

    @app.post("/api/specs", status_code=201, response_model=SpecView)
    def spec_upload(file: UploadFile = File(...)):
        if Path(file.filename or "").suffix.lower() not in {".md", ".txt"}:
            raise HTTPException(422, "text PRD required")
        data = file.file.read(1048577)
        if len(data) > 1048576:
            raise HTTPException(413, "PRD too large")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(422, "UTF-8 required") from None
        return backend.save_spec(text, Path(file.filename).name)

    @app.post("/api/projects/{pid}/run-previews", response_model=PreviewView)
    def preview(pid: str, body: Proposal):
        if pid != body.project_id:
            raise HTTPException(422, "project mismatch")
        return backend.preview(body)

    @app.post("/api/runs", status_code=202, response_model=SubmissionView)
    def submit(body: RunCreate, idempotency_key: str = Header(..., min_length=1, max_length=200)):
        return backend.submit(body, idempotency_key)

    @app.get("/api/runs", response_model=RunPage)
    def run_list(
        limit: int = Query(50, ge=1, le=100),
        cursor: str = "",
        state: str | None = None,
        project_id: str | None = None,
    ):
        return runs.queries.page(limit, cursor, state, project_id)

    @app.get("/api/notifications", response_model=NotificationPage)
    def notification_list(
        before: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=100)
    ):
        return notifications.page(before, limit)

    @app.post("/api/notifications/read", status_code=204)
    def notification_read(body: NotificationRead):
        notifications.mark_read(body.ids, body.through)
        return Response(status_code=204)

    @app.get("/api/dashboard", response_model=DashboardView)
    def dashboard():
        return runs.queries.dashboard()

    @app.get("/api/runs/{rid}", response_model=RunDetail)
    def run_detail(rid: str, response: Response):
        value = runs.queries.snapshot(rid)
        response.headers["ETag"] = f'"run:{rid}:v{value["version"]}"'
        return value

    @app.delete("/api/runs/{rid}", status_code=204)
    def delete_run(rid: str, request: Request, body: CleanupInput):
        backend.registered_run(rid)
        runs.delete(rid, version(request, rid), confirm=body.confirm)
        return Response(status_code=204)

    commands = Commands(runs)
    results = Results(runs.store)

    def command(rid, request, kind, body):
        backend.registered_run(rid)
        return commands.execute(
            rid, kind, version(request, rid), body, request.headers.get("idempotency-key")
        )

    @app.post("/api/runs/{rid}/cancel", status_code=202, response_model=OperationView)
    def cancel(rid: str, request: Request, body: Reason):
        return command(rid, request, "cancel", body.model_dump())

    @app.post("/api/runs/{rid}/pause", status_code=202, response_model=OperationView)
    def pause(rid: str, request: Request, body: Reason):
        return command(rid, request, "pause", body.model_dump())

    @app.post("/api/runs/{rid}/resume", status_code=202, response_model=OperationView)
    def resume(rid: str, request: Request, body: ResumeInput):
        return command(rid, request, "resume", body.model_dump())

    @app.post("/api/runs/{rid}/approve", response_model=OperationView)
    def approve(rid: str, request: Request, body: ApprovalInput):
        return command(rid, request, "approve", body.model_dump())

    @app.post("/api/runs/{rid}/reconcile", response_model=OperationView)
    def reconcile(rid: str, request: Request):
        return command(rid, request, "reconcile", {})

    @app.post("/api/runs/{rid}/request-changes", status_code=202, response_model=OperationView)
    def request_changes(rid: str, request: Request, body: ChangeInput):
        return command(rid, request, "request-changes", body.model_dump())

    @app.post("/api/runs/{rid}/defer", response_model=OperationView)
    def defer(rid: str, request: Request, body: DeferInput):
        return command(rid, request, "defer", body.model_dump())

    @app.post("/api/runs/{rid}/cleanup", status_code=202, response_model=OperationView)
    def cleanup(rid: str, request: Request, body: CleanupInput):
        return command(rid, request, "cleanup", body.model_dump())

    @app.post("/api/runs/{rid}/issues/{issue_id}/disposition", response_model=OperationView)
    def disposition(rid: str, issue_id: str, request: Request, body: DispositionInput):
        return command(
            rid, request, "issue-disposition", body.model_dump() | {"issue_id": issue_id}
        )

    @app.get("/api/operations/{op_id}", response_model=OperationView)
    def operation(op_id: str):
        return commands.get(op_id)

    @app.get("/api/runs/{rid}/tests", response_model=TestResultView)
    def test_results(rid: str, step_id: int | None = Query(None, ge=1)):
        return results.tests(rid, step_id)

    @app.get("/api/runs/{rid}/reviews", response_model=ReviewResultView)
    def review_results(rid: str, step_id: int | None = Query(None, ge=1)):
        return results.reviews(rid, step_id)

    @app.get("/api/runs/{rid}/changes", response_model=ChangesView)
    def changed_files(
        rid: str, candidate_sha: str | None = None, revision: int | None = Query(None, ge=0)
    ):
        return results.changes(rid, candidate_sha, revision)

    @app.get("/api/runs/{rid}/diff", response_model=DiffView)
    def file_diff(rid: str, file_id: str, candidate_sha: str, revision: int = Query(0, ge=0)):
        return results.diff(rid, file_id, candidate_sha, revision)

    @app.get("/api/runs/{rid}/artifacts/{aid}")
    def artifact(rid: str, aid: str):
        path, row = results.artifact(rid, aid)
        kind = row["kind"]
        suffix = (
            "xml"
            if kind == "junit"
            else "json"
            if kind == "changes"
            else "md"
            if kind == "report"
            else "diff"
        )
        return FileResponse(
            path,
            media_type="application/xml" if kind == "junit" else "text/plain; charset=utf-8",
            filename=aid + "." + suffix,
        )

    @app.get("/api/runs/{rid}/steps", response_model=StepPage)
    def steps(rid: str):
        rows = runs.queries.steps(rid)
        return {"items": [{k: v for k, v in r.items() if k != "data"} for r in rows]}

    @app.get("/api/runs/{rid}/agent-usage", response_model=AgentUsageView)
    def agent_usage(rid: str):
        return usage_view(runs.store, rid)

    @app.get("/api/runs/{rid}/event-history", response_model=EventPage)
    def events(rid: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        return runs.events.stream_page(rid, after, limit)

    @app.get("/api/runs/{rid}/spec", response_model=SpecContent)
    def run_spec(rid: str):
        value = runs.queries.get(rid)
        return {"text": value["spec"], "additional_instructions": value.get("answers", "")}

    @app.get("/api/runs/{rid}/logs", response_model=LogPage)
    def logs(
        rid: str,
        role: str | None = None,
        after: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
    ):
        if role is not None and role not in {"planner", "developer", "tester", "reviewer", "fixer"}:
            raise HTTPException(422, "invalid role")
        page = runs.events.stream_page(rid, after, limit)
        page["items"] = [
            e
            for e in page["items"]
            if e["kind"] in {"log.appended", "log.unavailable"}
            and (role is None or e["data"].get("role") == role)
        ]
        return {"schema_version": 2, **page}

    @app.get("/api/runs/{rid}/report", response_model=ReportView)
    def report(rid: str):
        return results.report(rid)

    @app.get("/api/runs/{rid}/logs/download")
    def logs_download(rid: str):
        runs.queries.get(rid)

        def chunks():
            cursor = 0
            while True:
                page = runs.events.stream_page(rid, cursor)
                for event in page["items"]:
                    if event["kind"] == "log.appended":
                        yield (
                            "[" + event["data"]["source"] + "] " + event["data"]["text"]
                        ).encode()
                    elif event["kind"] == "log.unavailable":
                        yield b"[unavailable log span]\n"
                if not page["has_more"]:
                    break
                cursor = page["next_cursor"]

        return StreamingResponse(
            chunks(),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="devorchestra-logs.txt"'},
        )

    from dev_orchestra.api.stream import routes

    app.include_router(routes(runs, security))
    from dev_orchestra.api.static import attach

    attach(app, static_directory)
    return app
