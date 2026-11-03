"""Explicit, retryable removal of terminal run records and owned files."""

import json
import re
import shutil
from pathlib import Path

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process_owner import alive, markers


class RunDeletionService:
    def __init__(self, store):
        self.store = store

    def paths(self, run):
        if not re.fullmatch(r"run-[0-9a-f]{16}", run["id"]):
            raise PipelineError("invalid run directory identity")
        config = Config.model_validate(run["config"])
        directory, worktree = Path(run["directory"]), Path(run["worktree"])
        expected = (
            config.storage.runs_directory / run["id"],
            config.storage.worktrees_directory / run["id"],
        )
        for path, owned in zip((directory, worktree), expected, strict=True):
            if path.absolute() != owned.absolute() or any(
                p.is_symlink() for p in (path, *path.parents)
            ):
                raise PipelineError("unsafe run deletion path")
            resolved = path.resolve()
            repo = Path(run["repo"]).resolve()
            if (
                resolved.is_relative_to(repo)
                or repo.is_relative_to(resolved)
                or self.store.path.resolve().is_relative_to(resolved)
            ):
                raise PipelineError("run deletion overlaps repository or database")
        if directory.resolve().is_relative_to(
            worktree.resolve()
        ) or worktree.resolve().is_relative_to(directory.resolve()):
            raise PipelineError("run deletion directories overlap")
        return directory, worktree

    def check_idle(self, db, run, directory):
        if run["state"] not in {"completed", "cancelled"}:
            raise ApplicationError(
                "INVALID_STATE", "only completed or cancelled runs can be deleted"
            )
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "jobs" in tables:
            for job in db.execute("SELECT * FROM jobs WHERE run_id=?", (run["id"],)):
                if job["state"] in {"queued", "claimed", "running"} or (
                    job["owner_pid"]
                    and alive({"pid": job["owner_pid"], "create_time": job["owner_create_time"]})
                ):
                    raise ApplicationError("RUN_BUSY", "Worker has not finished")
        if "operations" in tables and "run_id" in {
            r[1] for r in db.execute("PRAGMA table_info(operations)")
        }:
            if db.execute(
                "SELECT 1 FROM operations WHERE run_id=? AND status='accepted'", (run["id"],)
            ).fetchone():
                raise ApplicationError("RUN_BUSY", "run command is pending")
        for name in ("launch.json", "worker.json"):
            path = directory / name
            if path.exists():
                try:
                    owner = json.loads(path.read_text())
                    if (
                        not isinstance(owner, dict)
                        or not owner.get("pid")
                        or not owner.get("create_time")
                    ):
                        raise ValueError("missing ownership")
                    if alive(owner):
                        raise ApplicationError("RUN_BUSY", "Worker is still alive")
                except (OSError, ValueError, TypeError) as error:
                    raise PipelineError("cannot verify Worker ownership") from error
        for _, record in markers(directory):
            if not isinstance(record, dict):
                raise PipelineError("invalid process ownership record")
            if not record.get("finished") or (
                record.get("protocol") == 2 and not record.get("termination_confirmed")
            ):
                raise ApplicationError("RUN_BUSY", "process termination is not confirmed")

    def delete(self, run_id, expected_version, *, confirm=False):
        if confirm is not True:
            raise PipelineError("explicit deletion confirmation required")
        run = self.store.get(run_id)
        directory, worktree = self.paths(run)
        # The outer lock survives directory removal and serializes retries.
        deletion_lock = self.store.path.parent / ".run-deletions" / (run_id + ".lock")
        report_lock = self.store.path.parent / ".run-deletions" / (run_id + ".report.lock")
        with lock(deletion_lock), lock(directory / "execution.lock"), lock(report_lock):
            with self.store.transaction() as db:
                run = self.store.get(run_id)
                if run["version"] != expected_version:
                    raise ApplicationError("VERSION_CONFLICT", "run version changed")
                self.paths(run)
                self.check_idle(db, run, directory)
                if not run.get("deletion_pending"):
                    self.store.update(
                        run_id, deletion_pending=True, expected_version=expected_version
                    )
            # Preserve the row on any filesystem failure. A subsequent confirmed
            # request uses the latest version and repeats the same safe removal.
            try:
                if worktree.exists():
                    Git(Path(run["repo"])).remove(worktree)  # Never force or delete the branch.
                with self.store.transaction() as db:
                    self.check_idle(db, self.store.get(run_id), directory)
                    shutil.rmtree(directory)
                    self.delete_rows(db, run_id)
            except OSError as error:
                raise ApplicationError(
                    "RUN_DELETE_FAILED",
                    "file removal failed; retry deletion with the latest version",
                ) from error

    def delete_rows(self, db, run_id):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "review_issues" in tables:
            for table in ("issue_assessments", "issue_dispositions"):
                db.execute(
                    f"DELETE FROM {table} WHERE issue_id IN (SELECT id FROM review_issues WHERE run_id=?)",
                    (run_id,),
                )
        if "log_spans" in tables:
            db.execute(
                "DELETE FROM log_spans WHERE log_id IN (SELECT id FROM log_sources WHERE run_id=?)",
                (run_id,),
            )
        spec = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        for table in (
            "snapshots",
            "reports",
            "review_issues",
            "artifacts",
            "log_sources",
            "run_directives",
            "jobs",
            "operations",
            "approvals",
            "events",
            "steps",
        ):
            if table in tables and "run_id" in {
                r[1] for r in db.execute(f"PRAGMA table_info({table})")
            }:
                db.execute(f"DELETE FROM {table} WHERE run_id=?", (run_id,))
        db.execute("DELETE FROM runs WHERE id=?", (run_id,))
        if "spec_id" in spec.keys() and spec["spec_id"]:
            db.execute(
                "DELETE FROM specs WHERE id=? AND NOT EXISTS(SELECT 1 FROM runs WHERE spec_id=?)",
                (spec["spec_id"], spec["spec_id"]),
            )
