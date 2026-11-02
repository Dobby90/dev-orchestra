"""Typer adapter. All workflow decisions belong to shared services/runner."""
import functools
import json
from pathlib import Path
import time
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
def setup(ctx: typer.Context, config: Path | None = typer.Option(None, "--config"), database: Path | None = typer.Option(None, "--database")):
    settings = load_config(config)
    if database:
        settings.storage.database = database.expanduser().resolve()
    ctx.obj = RunService(settings)


@app.command()
@guarded
def run(ctx: typer.Context, repo: Path = typer.Option(...), spec: Path = typer.Option(...), base: str | None = None, max_fix_attempts: int | None = typer.Option(None, min=0), detach: bool = False):
    service = ctx.obj
    run_id = service.create(repo, spec, base, max_fix_attempts)
    typer.echo(run_id)
    service.start(run_id, detached=detach)
    if not detach:
        result = service.store.get(run_id)
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
    typer.echo(json.dumps(ctx.obj.store.list(), ensure_ascii=False, indent=2))


@app.command()
@guarded
def resume(ctx: typer.Context, run_id: str, answers: Path | None = None, max_fix_attempts: int | None = typer.Option(None, min=0), detach: bool = False):
    ctx.obj.resume(run_id, answers, max_fix_attempts)
    ctx.obj.start(run_id, detached=detach)
    result = ctx.obj.store.get(run_id)
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
def report(ctx: typer.Context, run_id: str):
    typer.echo(ctx.obj.report(run_id))


@app.command()
@guarded
def logs(ctx: typer.Context, run_id: str, follow: bool = False, role: str | None = None):
    positions = {}
    while True:
        for path in ctx.obj.log_paths(run_id, role):
            with path.open() as handle:
                handle.seek(positions.get(path, 0))
                text = handle.read()
                positions[path] = handle.tell()
            if text:
                typer.echo(f"[{path.name}]\n{text}", nl=False)
        state = ctx.obj.store.get(run_id)["state"]
        if not follow or state in {"waiting_approval", "completed", "failed", "cancelled", "paused", "needs_input"}:
            break
        time.sleep(.3)


@app.command()
@guarded
def doctor(ctx: typer.Context):
    from dev_orchestra.infrastructure.codex import CodexClient
    from dev_orchestra.infrastructure.test_runner import TestRunner
    CodexClient(ctx.obj.config.codex).check()
    TestRunner().check(Path.cwd())
    typer.echo("Codex exec capabilities and OS test sandbox verified (no model call)")
