"""Local REST adapter. Run with uvicorn --factory dev_orchestra.api.main:create_app."""

import hmac
import ipaddress
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from dev_orchestra.api.security import COOKIE, Security, digest, runtime_key
from dev_orchestra.application.backend import BackendService
from dev_orchestra.application.contracts import ApprovalCommand
from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.service import RunService
from dev_orchestra.application.web_contracts import (
    ApprovalInput,
    ProjectCreate,
    ProjectPage,
    ProjectPatch,
    ProjectView,
    Proposal,
    Reason,
    RunCreate,
    SessionCreate,
    SessionView,
    SubmissionView,
)
from dev_orchestra.config import load_config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.infrastructure.process import sanitize


def create_app(config=None, allowed_roots=None, port=8000):
    config = config or load_config(
        Path(os.environ["DEVORCHESTRA_CONFIG"]) if "DEVORCHESTRA_CONFIG" in os.environ else None
    )
    new = not config.storage.database.exists()
    runs = RunService(config)
    if new:
        upgrade(runs.store.path)
    with runs.store.connection() as db:
        if db.execute("SELECT version FROM schema_version").fetchone()[0] != 3:
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
        if request.url.path != "/api/health":
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
            if request.url.path != "/api/session" or request.method != "POST":
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
                    and not (request.url.path == "/api/specs" and kind == "multipart/form-data")
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
        response.headers["Cache-Control"] = "no-store"
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
            COOKIE, token, httponly=True, samesite="strict", path="/api", max_age=28800
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

    @app.post("/api/projects", status_code=201, response_model=ProjectView)
    def project_create(body: ProjectCreate):
        return backend.register(body)

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

    @app.get("/api/projects/{pid}/branches")
    def branches(pid: str):
        from dev_orchestra.infrastructure.git import Git

        p = backend.project(pid)
        backend.projects.validate_repository(Path(p["repository_path"]), p["default_base_branch"])
        return {
            "items": Git(Path(p["repository_path"]))
            .call("for-each-ref", "--format=%(refname:short)", "refs/heads/")
            .splitlines()
        }

    @app.post("/api/specs", status_code=201)
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

    @app.post("/api/projects/{pid}/run-previews")
    def preview(pid: str, body: Proposal):
        if pid != body.project_id:
            raise HTTPException(422, "project mismatch")
        return backend.preview(body)

    @app.post("/api/runs", status_code=202, response_model=SubmissionView)
    def submit(body: RunCreate, idempotency_key: str = Header(..., min_length=1, max_length=200)):
        return backend.submit(body, idempotency_key)

    @app.get("/api/runs")
    def run_list(
        limit: int = Query(50, ge=1, le=100),
        cursor: str = "",
        state: str | None = None,
        project_id: str | None = None,
    ):
        return runs.queries.page(limit, cursor, state, project_id)

    @app.get("/api/dashboard")
    def dashboard():
        return runs.queries.dashboard()

    @app.get("/api/runs/{rid}")
    def run_detail(rid: str, response: Response):
        value = runs.queries.snapshot(rid)
        response.headers["ETag"] = f'"run:{rid}:v{value["version"]}"'
        return value

    @app.post("/api/runs/{rid}/cancel", status_code=202)
    def cancel(rid: str, request: Request, body: Reason):
        check_run(request, rid)
        runs.cancel(rid, expected_version=version(request, rid))
        with runs.store.connection() as db:
            db.execute(
                "UPDATE jobs SET state='cancelled' WHERE run_id=? AND state='queued'", (rid,)
            )
        return {"state": runs.queries.get(rid)["state"]}

    @app.post("/api/runs/{rid}/pause", status_code=202)
    def pause(rid: str, request: Request, body: Reason):
        check_run(request, rid)
        runs.cancel(rid, pause=True, expected_version=version(request, rid))
        with runs.store.connection() as db:
            db.execute(
                "UPDATE jobs SET state='cancelled' WHERE run_id=? AND state='queued'", (rid,)
            )
        return {"state": runs.queries.get(rid)["state"]}

    @app.post("/api/runs/{rid}/approve")
    def approve(rid: str, request: Request, body: ApprovalInput):
        check_run(request, rid)
        runs.approvals.approve_command(
            ApprovalCommand(
                run_id=rid, expected_version=version(request, rid), expected_sha=body.expected_sha
            )
        )
        return runs.queries.summary(rid).model_dump(mode="json")

    @app.post("/api/runs/{rid}/reconcile")
    def reconcile(rid: str, request: Request):
        check_run(request, rid)
        runs.status(rid)
        return runs.queries.summary(rid).model_dump(mode="json")

    @app.get("/api/runs/{rid}/steps")
    def steps(rid: str):
        rows = runs.queries.steps(rid)
        return {"items": [{k: v for k, v in r.items() if k != "data"} for r in rows]}

    @app.get("/api/runs/{rid}/event-history")
    def events(rid: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
        page = runs.events.history(rid, after, limit)
        for item in page["items"]:
            item["data"] = sanitize(
                {k: v for k, v in json.loads(item["data"]).items() if k != "artifact"}
            )
        return page

    @app.get("/api/runs/{rid}/spec")
    def run_spec(rid: str):
        value = runs.queries.get(rid)
        return {"text": value["spec"], "additional_instructions": value.get("answers", "")}

    @app.get("/api/runs/{rid}/logs")
    def logs(rid: str, role: str | None = None):
        return sanitize(runs.events.log_chunks(rid, role=role))

    @app.get("/api/runs/{rid}/report")
    def report(rid: str):
        path = Path(runs.queries.get(rid)["directory"]) / "final-report.md"
        if not path.is_file():
            raise HTTPException(404, "report not available")
        return {"markdown": sanitize(path.read_text())}

    return app
