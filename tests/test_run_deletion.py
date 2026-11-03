"""Deletion safety and recovery use disposable repositories, never model calls."""

import json
import os
import shutil
from pathlib import Path

import pytest
from test_backend_api import project
from test_backend_api import web as web
from test_gui_integration import completed as completed
from test_workflow import service as service
from typer.testing import CliRunner

from dev_orchestra.application.commands import Commands
from dev_orchestra.application.deletion import RunDeletionService
from dev_orchestra.application.queue import QueueService
from dev_orchestra.cli import app
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.infrastructure.process_owner import identity


def delete(svc, rid):
    svc.delete(rid, svc.store.get(rid)["version"], confirm=True)


def simulate_worker_exit(run):
    # The fixture executes Runner inside pytest. Model a stopped Worker by
    # retaining its PID with a different birth time (PID reuse must be safe).
    path = Path(run["directory"]) / "worker.json"
    owner = json.loads(path.read_text())
    write(path, owner | {"create_time": owner["create_time"] - 1})


@pytest.mark.parametrize("schema", [2, 5])
def test_cancelled_deletion_preserves_other_runs_and_repository(service, schema):
    svc, rid, repo = service
    if schema == 5:
        upgrade(svc.store.path)
    other = svc.create(repo, Path(svc.store.get(rid)["directory"]) / "spec.md")
    before = (repo / "main.py").read_bytes()
    svc.cancel(rid)
    run = svc.store.get(rid)
    cursor = svc.events.high_water()
    delete(svc, rid)
    assert not Path(run["directory"]).exists()
    assert svc.store.get(other)["state"] == "queued"
    assert (repo / "main.py").read_bytes() == before
    assert svc.events.high_water() >= cursor
    if schema == 5:
        assert svc.queries.snapshot(other)["event_cursor"] == svc.events.high_water()
    with pytest.raises(PipelineError, match="unknown run"):
        svc.store.get(rid)
    with svc.store.connection() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()


def test_completed_deletion_removes_evidence_but_keeps_branch(completed):
    svc, rid, repo, _ = completed
    svc.approve(rid)
    run = svc.store.get(rid)
    original = (repo / "main.py").read_bytes()
    branch_sha = Git(repo).call("rev-parse", "refs/heads/" + run["branch"])
    # Include nested log foreign keys, review assessments and operation history.
    Commands(svc).execute(rid, "reconcile", run["version"], {}, "reconcile")
    simulate_worker_exit(run)
    with svc.store.connection() as db:
        step = db.execute("SELECT id FROM steps WHERE run_id=? LIMIT 1", (rid,)).fetchone()[0]
        db.execute(
            "INSERT INTO review_issues VALUES('issue',?,'fingerprint',?,'low','{}','unresolved')",
            (rid, step),
        )
        db.execute(
            "INSERT INTO issue_assessments VALUES('issue',?,'unresolved','evidence',?)",
            (step, branch_sha),
        )
        db.execute(
            "INSERT INTO issue_dispositions(issue_id,step_id,status,reason,created_at) VALUES('issue',?,'dismissed','reason','timestamp')",
            (step,),
        )
        db.execute(
            "INSERT INTO run_directives VALUES('directive',?,0,'test','instructions','timestamp')",
            (rid,),
        )
        db.execute(
            "INSERT INTO log_sources(id,run_id,relative_path,generation,identity) VALUES('log',?,'test.log',0,'test')",
            (rid,),
        )
        svc.store._event(db, rid, "log.appended", {})
        event = db.execute("SELECT max(id) FROM events WHERE run_id=?", (rid,)).fetchone()[0]
        db.execute("INSERT INTO log_spans VALUES(?, 'log',0,1,'hash','info')", (event,))
    delete(svc, rid)
    assert not Path(run["directory"]).exists()
    assert not Path(run["worktree"]).exists()
    assert Git(repo).call("rev-parse", "refs/heads/" + run["branch"]) == branch_sha
    assert (repo / "main.py").read_bytes() == original
    with svc.store.connection() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        for table in (
            "steps",
            "events",
            "approvals",
            "artifacts",
            "snapshots",
            "reports",
            "operations",
            "log_sources",
            "review_issues",
            "run_directives",
        ):
            assert (
                db.execute(f"SELECT count(*) FROM {table} WHERE run_id=?", (rid,)).fetchone()[0]
                == 0
            )


@pytest.mark.parametrize(
    "state", ["queued", "planning", "failed", "paused", "needs_input", "waiting_approval"]
)
def test_nonterminal_runs_cannot_be_deleted(service, state):
    svc, rid, _ = service
    with svc.store.connection() as db:
        db.execute("UPDATE runs SET state=? WHERE id=?", (state, rid))
    with pytest.raises(PipelineError, match="only completed or cancelled"):
        delete(svc, rid)
    assert not svc.store.get(rid).get("deletion_pending")


def test_confirmation_version_and_execution_lock(service):
    svc, rid, _ = service
    svc.cancel(rid)
    run = svc.store.get(rid)
    with pytest.raises(PipelineError, match="confirmation"):
        svc.delete(rid, run["version"])
    with pytest.raises(PipelineError, match="version changed"):
        svc.delete(rid, run["version"] - 1, confirm=True)
    with lock(Path(run["directory"]) / "execution.lock"):
        with pytest.raises(PipelineError, match="owned"):
            delete(svc, rid)


def test_report_generation_blocks_deletion(service):
    svc, rid, _ = service
    svc.cancel(rid)
    report_lock = svc.store.path.parent / ".run-deletions" / (rid + ".report.lock")
    with lock(report_lock):
        with pytest.raises(PipelineError, match="owned"):
            delete(svc, rid)
    assert not svc.store.get(rid).get("deletion_pending")
    delete(svc, rid)


@pytest.mark.parametrize("state", ["queued", "claimed", "running"])
def test_active_jobs_block_deletion(service, state):
    svc, rid, _ = service
    upgrade(svc.store.path)
    svc.cancel(rid)
    jid = QueueService(svc.store).enqueue(rid)
    with svc.store.connection() as db:
        db.execute("UPDATE jobs SET state=? WHERE id=?", (state, jid))
    with pytest.raises(PipelineError, match="Worker"):
        delete(svc, rid)


def test_pending_operation_blocks_deletion(service):
    svc, rid, _ = service
    upgrade(svc.store.path)
    svc.cancel(rid)
    with svc.store.transaction() as db:
        Commands(svc).record(db, rid, "reconcile", "test", "key", "hash", "accepted", {})
    with pytest.raises(PipelineError, match="pending"):
        delete(svc, rid)


@pytest.mark.parametrize("source", ["launch.json", "worker.json", "job", "process"])
def test_live_or_unconfirmed_owners_block_deletion(service, source):
    svc, rid, _ = service
    upgrade(svc.store.path)
    svc.cancel(rid)
    run = svc.store.get(rid)
    owner = identity(os.getpid())
    if source == "job":
        jid = QueueService(svc.store).enqueue(rid)
        with svc.store.connection() as db:
            db.execute(
                "UPDATE jobs SET state='finished',owner_pid=?,owner_create_time=? WHERE id=?",
                (owner["pid"], owner["create_time"], jid),
            )
    elif source == "process":
        write(
            Path(run["directory"]) / "processes/test.json",
            {"finished": True, "protocol": 2, "termination_confirmed": False},
        )
    else:
        write(Path(run["directory"]) / source, owner)
    with pytest.raises(PipelineError):
        delete(svc, rid)
    assert not svc.store.get(rid).get("deletion_pending")


def test_dirty_worktree_is_preserved_and_retry_succeeds(completed):
    svc, rid, _, _ = completed
    svc.approve(rid)
    run = svc.store.get(rid)
    simulate_worker_exit(run)
    changed = Path(run["worktree"]) / "main.py"
    previous = changed.read_text()
    changed.write_text("uncommitted\n")
    with pytest.raises(PipelineError):
        delete(svc, rid)
    assert changed.read_text() == "uncommitted\n"
    assert svc.store.get(rid)["deletion_pending"]
    changed.write_text(previous)
    delete(svc, rid)


def test_partial_file_failure_can_be_retried_after_service_restart(service, monkeypatch):
    svc, rid, _ = service
    svc.cancel(rid)
    run = svc.store.get(rid)
    directory = Path(run["directory"])
    original = shutil.rmtree

    def partial(path):
        (path / "spec.md").unlink()
        raise PermissionError("injected")

    monkeypatch.setattr("dev_orchestra.application.deletion.shutil.rmtree", partial)
    with pytest.raises(PipelineError, match="retry deletion"):
        delete(svc, rid)
    pending = svc.store.get(rid)
    assert pending["deletion_pending"] and pending["version"] == run["version"] + 1
    assert not (directory / "spec.md").exists()
    with pytest.raises(PipelineError, match="deletion is pending"):
        svc.store.update(rid, error="change")
    with pytest.raises(PipelineError, match="deletion is pending"):
        svc.report(rid)
    monkeypatch.setattr("dev_orchestra.application.deletion.shutil.rmtree", original)
    from dev_orchestra.application.service import RunService

    delete(RunService(svc.config), rid)
    assert not directory.exists()


def test_unsafe_paths_and_symlinks_are_rejected(service, tmp_path):
    svc, rid, repo = service
    svc.cancel(rid)
    run = svc.store.get(rid)
    svc.store.update(rid, directory=str(repo))
    with pytest.raises(PipelineError, match="unsafe"):
        delete(svc, rid)
    svc.store.update(rid, directory=run["directory"])
    directory = Path(run["directory"])
    moved = tmp_path / "preserved"
    directory.rename(moved)
    directory.symlink_to(moved, target_is_directory=True)
    with pytest.raises(PipelineError, match="unsafe"):
        delete(svc, rid)
    assert (moved / "spec.md").exists()


def test_api_delete_requires_confirmation_version_and_security(web):
    c, svc, rid, repo, _ = web
    p = project(c, svc, repo)
    with svc.store.connection() as db:
        db.execute("UPDATE runs SET project_id=? WHERE id=?", (p["id"], rid))
    svc.cancel(rid)
    run = svc.store.get(rid)
    url = f"/api/runs/{rid}"
    headers = {"If-Match": f'"run:{rid}:v{run["version"]}"'}
    assert c.request("DELETE", url, json={"confirm": True}).status_code == 428
    assert c.request("DELETE", url, headers=headers, json={"confirm": False}).status_code == 422
    assert (
        c.request(
            "DELETE", url, headers=headers | {"X-CSRF-Token": "invalid"}, json={"confirm": True}
        ).status_code
        == 403
    )
    assert (
        c.request(
            "DELETE", url, headers={"If-Match": f'"run:{rid}:v0"'}, json={"confirm": True}
        ).status_code
        == 412
    )
    response = c.request("DELETE", url, headers=headers, json={"confirm": True})
    assert response.status_code == 204, response.text
    assert c.get(url).status_code == 404
    assert c.get("/api/runs").json()["items"] == []
    assert not c.get("/api/dashboard").json()["counts_by_state"]


def test_cli_confirmation_and_deletion(service, tmp_path):
    svc, rid, _ = service
    svc.cancel(rid)
    config = tmp_path / "config.json"
    config.write_text(json.dumps(svc.config.model_dump(mode="json")))
    args = ["--config", str(config), "delete", rid]
    runner = CliRunner()
    assert runner.invoke(app, args, input="n\n").exit_code != 0
    assert svc.store.get(rid)
    assert runner.invoke(app, args + ["--yes"]).exit_code == 0


def test_failure_after_files_removed_keeps_retryable_row(service, monkeypatch):
    svc, rid, _ = service
    svc.cancel(rid)
    original = RunDeletionService.delete_rows

    def fail(*args):
        raise OSError("injected database phase failure")

    monkeypatch.setattr(RunDeletionService, "delete_rows", fail)
    with pytest.raises(PipelineError):
        delete(svc, rid)
    assert svc.store.get(rid)["deletion_pending"]
    monkeypatch.setattr(RunDeletionService, "delete_rows", original)
    delete(svc, rid)
