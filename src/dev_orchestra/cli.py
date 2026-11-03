"""Typer adapter. All workflow decisions belong to shared services/runner."""

import functools
import json
import time
from pathlib import Path

import typer
from pydantic import ValidationError

from dev_orchestra.application.service import RunService
from dev_orchestra.config import load_config
from dev_orchestra.domain.models import PipelineError

app = typer.Typer(no_args_is_help=True, help="Local Codex development orchestration")


def guarded(function):
    @functools.wraps(function)
    def invoke(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except (PipelineError, ValidationError, OSError, ValueError) as error:
            from dev_orchestra.infrastructure.process import redact

            typer.echo(redact(str(error)), err=True)
            raise typer.Exit(1) from error

    return invoke


@app.callback()
@guarded
def setup(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config"),
    database: Path | None = typer.Option(None, "--database"),
):
    settings = load_config(config)
    if database:
        settings.storage.database = database.expanduser().resolve()
    ctx.obj = RunService(settings)


@app.command()
@guarded
def run(
    ctx: typer.Context,
    repo: Path = typer.Option(...),
    spec: Path = typer.Option(...),
    base: str | None = None,
    max_fix_attempts: int | None = typer.Option(None, min=0),
    detach: bool = False,
):
    service = ctx.obj
    run_id = service.create(repo, spec, base, max_fix_attempts)
    typer.echo(run_id)
    service.start(run_id, detached=detach)
    if not detach:
        result = service.queries.get(run_id)
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        if result["state"] in {"failed", "needs_input", "cancelled"}:
            raise typer.Exit(2)


@app.command()
@guarded
def status(ctx: typer.Context, run_id: str):
    typer.echo(json.dumps(ctx.obj.status(run_id), ensure_ascii=False, indent=2))


@app.command("list")
@guarded
def list_runs(ctx: typer.Context):
    typer.echo(json.dumps(ctx.obj.queries.list(), ensure_ascii=False, indent=2))


@app.command()
@guarded
def resume(
    ctx: typer.Context,
    run_id: str,
    answers: Path | None = None,
    max_fix_attempts: int | None = typer.Option(None, min=0),
    detach: bool = False,
):
    ctx.obj.resume(run_id, answers, max_fix_attempts)
    ctx.obj.start(run_id, detached=detach)
    result = ctx.obj.queries.get(run_id)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not detach and result["state"] in {"failed", "needs_input"}:
        raise typer.Exit(2)


@app.command("test-changes")
@guarded
def test_changes(ctx: typer.Context, run_id: str):
    """승인 대기 중인 기존 테스트 변경 사유와 전체 패치를 조회합니다."""
    from dev_orchestra.application.test_changes import TestChangeService

    typer.echo(
        json.dumps(TestChangeService(ctx.obj.store).view(run_id), ensure_ascii=False, indent=2)
    )


@app.command("approve-test-changes")
@guarded
def approve_test_changes(
    ctx: typer.Context,
    run_id: str,
    proposal_hash: str = typer.Option(...),
    input_sha: str = typer.Option(...),
    detach: bool = False,
):
    """검토한 정확한 패치를 승인하고 테스트·리뷰까지 실행을 재개합니다."""
    ctx.obj.decide_test_changes(run_id, proposal_hash, input_sha, True)
    ctx.obj.start(run_id, detached=detach)
    result = ctx.obj.queries.get(run_id)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not detach and result["state"] in {"failed", "needs_input"}:
        raise typer.Exit(2)


@app.command("reject-test-changes")
@guarded
def reject_test_changes(
    ctx: typer.Context,
    run_id: str,
    proposal_hash: str = typer.Option(...),
    input_sha: str = typer.Option(...),
    reason: str = typer.Option(...),
    detach: bool = False,
):
    """패치를 반려하고 사유를 전달해 해당 단계에서 새 패치를 생성합니다."""
    ctx.obj.decide_test_changes(run_id, proposal_hash, input_sha, False, reason)
    ctx.obj.start(run_id, detached=detach)
    result = ctx.obj.queries.get(run_id)
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    if not detach and result["state"] in {"failed", "needs_input"}:
        raise typer.Exit(2)


@app.command()
@guarded
def cancel(ctx: typer.Context, run_id: str):
    ctx.obj.cancel(run_id)
    typer.echo("Cancellation requested")


@app.command()
@guarded
def pause(ctx: typer.Context, run_id: str):
    ctx.obj.cancel(run_id, pause=True)
    typer.echo("Pause requested")


@app.command()
@guarded
def approve(ctx: typer.Context, run_id: str):
    ctx.obj.approve(run_id)
    typer.echo("Approved; no push or merge performed")


@app.command()
@guarded
def cleanup(ctx: typer.Context, run_id: str):
    ctx.obj.cleanup(run_id)
    typer.echo("Worktree removed; branch and reports preserved")


@app.command()
@guarded
def delete(
    ctx: typer.Context,
    run_id: str,
    yes: bool = typer.Option(False, "--yes", help="Confirm permanent record and file deletion"),
):
    if not yes:
        typer.confirm(
            "작업 기록·로그·보고서·승인 근거와 Worktree를 영구 삭제합니다. "
            "원본 저장소와 작업 브랜치는 보존합니다. 계속할까요?",
            abort=True,
        )
    run = ctx.obj.queries.get(run_id)
    ctx.obj.delete(run_id, run["version"], confirm=True)
    typer.echo("작업 기록 삭제 완료; 원본 저장소와 작업 브랜치는 보존했습니다")


@app.command()
@guarded
def report(ctx: typer.Context, run_id: str):
    typer.echo(ctx.obj.report(run_id))


@app.command()
@guarded
def logs(ctx: typer.Context, run_id: str, follow: bool = False, role: str | None = None):
    positions = {}
    while True:
        page = ctx.obj.log_chunks(run_id, positions, role)
        positions = page["cursor"]
        for chunk in page["chunks"]:
            typer.echo(f"[{Path(chunk['log_id']).name}]\n{chunk['text']}", nl=False)
        state = ctx.obj.queries.get(run_id)["state"]
        # Drain all persisted chunks before ending a one-shot/terminal read.
        if page["chunks"] and (
            not follow
            or state
            in {"waiting_approval", "completed", "failed", "cancelled", "paused", "needs_input"}
        ):
            continue
        if not follow or state in {
            "waiting_approval",
            "completed",
            "failed",
            "cancelled",
            "paused",
            "needs_input",
        }:
            break
        time.sleep(0.3)


@app.command()
@guarded
def doctor(ctx: typer.Context):
    from dev_orchestra.infrastructure.codex import CodexClient
    from dev_orchestra.infrastructure.test_runner import TestRunner

    CodexClient(ctx.obj.config.codex).check()
    TestRunner().check(Path.cwd())
    typer.echo("Codex exec capabilities and OS test sandbox verified (no model call)")


@app.command("database-upgrade")
@guarded
def database_upgrade(ctx: typer.Context):
    from dev_orchestra.infrastructure.migrations import upgrade

    backup = upgrade(ctx.obj.store.path)
    typer.echo(str(backup) if backup else "Database schema is current")


@app.command("web-session")
@guarded
def web_session(ctx: typer.Context):
    from dev_orchestra.api.security import bootstrap

    with ctx.obj.store.connection() as db:
        if db.execute("SELECT version FROM schema_version").fetchone()[0] != 5:
            raise PipelineError("run database-upgrade first")
    typer.echo(bootstrap(ctx.obj.store))


@app.command("api-start")
@guarded
def api_start(
    ctx: typer.Context,
    allow_root: list[Path] = typer.Option([], "--allow-root"),
    port: int = typer.Option(8000, min=1024, max=65535),
):
    try:
        import uvicorn

        from dev_orchestra.api.main import create_app
    except ImportError as error:
        raise PipelineError("install the web extra: uv sync --extra web") from error
    uvicorn.run(
        create_app(ctx.obj.config, allow_root, port),
        host="127.0.0.1",
        port=port,
        proxy_headers=False,
    )


worker_app = typer.Typer(help="Independent local queue Dispatcher")
app.add_typer(worker_app, name="worker")


@worker_app.command("start")
@guarded
def worker_start(ctx: typer.Context, detach: bool = False):
    from dev_orchestra.application.queue import QueueService

    queue = QueueService(ctx.obj.store)
    if not queue.enabled():
        raise PipelineError("run database-upgrade first")
    if queue.dispatcher_status()["alive"]:
        raise PipelineError("Dispatcher is already running")
    if not detach:
        from dev_orchestra.execution.dispatcher import Dispatcher

        Dispatcher(ctx.obj.store).run()
        return
    import subprocess
    import sys

    output_path = ctx.obj.store.path.with_suffix(".dispatcher.log")
    with output_path.open("a") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", "dev_orchestra.execution.dispatcher", str(ctx.obj.store.path)],
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = queue.dispatcher_status()
        if state["alive"]:
            typer.echo(json.dumps(state))
            return
        if child.poll() is not None:
            raise PipelineError("Dispatcher failed to start; inspect its local log")
        time.sleep(0.05)
    raise PipelineError("Dispatcher startup not confirmed; inspect worker status")


@worker_app.command("status")
@guarded
def worker_status(ctx: typer.Context):
    from dev_orchestra.application.queue import QueueService

    queue = QueueService(ctx.obj.store)
    if not queue.enabled():
        raise PipelineError("run database-upgrade first")
    typer.echo(json.dumps(queue.dispatcher_status()))


@worker_app.command("stop")
@guarded
def worker_stop(ctx: typer.Context):
    from dev_orchestra.application.queue import QueueService

    if not QueueService(ctx.obj.store).enabled():
        raise PipelineError("run database-upgrade first")
    with ctx.obj.store.connection() as db:
        db.execute("UPDATE dispatchers SET stop_requested=1 WHERE slot=1")
    typer.echo("Dispatcher stop requested; active Workers continue")


web_app = typer.Typer(help="로컬 GUI·API·Dispatcher 관리")
app.add_typer(web_app, name="web")


@web_app.command("start")
@guarded
def web_start(
    ctx: typer.Context,
    allow_root: list[Path] = typer.Option([], "--allow-root"),
    port: int = typer.Option(8000, min=1024, max=65535),
    detach: bool = False,
    assets: Path | None = typer.Option(None),
):
    from dev_orchestra.application.web_runtime import WebRuntime

    typer.echo(json.dumps(WebRuntime(ctx.obj).start(allow_root, port, detach, assets)))


@web_app.command("status")
@guarded
def web_status(ctx: typer.Context):
    from dev_orchestra.application.web_runtime import WebRuntime

    typer.echo(json.dumps(WebRuntime(ctx.obj).status()))


@web_app.command("stop")
@guarded
def web_stop(ctx: typer.Context, cancel_active: bool = False):
    from dev_orchestra.application.web_runtime import WebRuntime

    typer.echo(json.dumps(WebRuntime(ctx.obj).stop(cancel_active)))


@app.command("request-changes")
@guarded
def request_changes(
    ctx: typer.Context,
    run_id: str,
    instructions: Path = typer.Option(...),
    new_max_fix_attempts: int | None = typer.Option(None, min=1),
    detach: bool = False,
):
    from dev_orchestra.application.commands import Commands

    run = ctx.obj.store.get(run_id)
    result = Commands(ctx.obj).execute(
        run_id,
        "request-changes",
        run["version"],
        {
            "expected_sha": run.get("final_sha"),
            "instructions": instructions.read_text(),
            "new_max_fix_attempts": new_max_fix_attempts,
        },
    )
    typer.echo(json.dumps(result))
    ctx.obj.start(run_id, detached=detach)


@app.command("defer")
@guarded
def defer(ctx: typer.Context, run_id: str, reason: str = typer.Option(...)):
    from dev_orchestra.application.commands import Commands

    run = ctx.obj.store.get(run_id)
    typer.echo(
        json.dumps(Commands(ctx.obj).execute(run_id, "defer", run["version"], {"reason": reason}))
    )
