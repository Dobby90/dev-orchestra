"""Explicit, retryable removal of terminal run records and owned files."""

import json
import re
import shutil
from pathlib import Path

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.coordination import metadata, retire_run_capacity
from dev_orchestra.infrastructure.files import contained
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process_owner import alive, markers
from dev_orchestra.infrastructure.test_runner import junit_paths


class RunDeletionService:
    def __init__(self, store):
        self.store = store

    def branch_target(self, run, worktree):
        git = Git(Path(run["repo"]))
        branch = run["branch"]
        git.call("check-ref-format", "--branch", branch)
        if branch == run["base"]:
            raise ApplicationError("RUN_BRANCH_CHANGED", "cannot delete the base branch")
        ref = "refs/heads/" + branch
        if worktree.exists():
            checkout = Git(worktree)
            expected = run.get("final_sha") or run.get("current_sha") or run["base_sha"]
            if (
                checkout.call("rev-parse", "--symbolic-full-name", "HEAD") != ref
                or checkout.sha() != expected
                or checkout.common().resolve() != git.common().resolve()
            ):
                raise ApplicationError("RUN_BRANCH_CHANGED", "worktree branch changed")
        with self.store.connection() as db:
            for row in db.execute("SELECT id,data FROM runs WHERE id<>?", (run["id"],)):
                other = json.loads(row["data"])
                if (
                    other.get("branch") == branch
                    and Git(Path(other["repo"])).common().resolve() == git.common().resolve()
                ):
                    raise ApplicationError("RUN_BRANCH_CHANGED", "branch belongs to another run")
        # Query an exact ref, including symbolic-ref information; an absent branch
        # is valid both before preparation and after a partially completed deletion.
        refs = git.call("for-each-ref", "--format=%(refname) %(objectname) %(symref)", ref)
        target = None
        for line in refs.splitlines():
            parts = line.split()
            if parts[0] == ref:
                if len(parts) != 2:
                    raise ApplicationError("RUN_BRANCH_CHANGED", "symbolic branch is not owned")
                target = parts[1]
        if target is None:
            return None
        expected = run.get("final_sha") or run.get("current_sha") or run["base_sha"]
        if target != expected:
            raise ApplicationError("RUN_BRANCH_CHANGED", "branch changed after run execution")
        for entry in git.call("worktree", "list", "--porcelain", "-z").split("\0\0"):
            fields = entry.strip("\0").split("\0")
            if "branch " + ref in fields:
                path = Path(fields[0].removeprefix("worktree "))
                if path.resolve() != worktree.resolve():
                    raise ApplicationError("RUN_BRANCH_CHANGED", "branch is checked out elsewhere")
        if not worktree.exists() and not (
            run.get("current_sha") or run.get("final_sha") or run.get("deletion_pending")
        ):
            raise ApplicationError("RUN_BRANCH_CHANGED", "branch ownership is not established")
        return target

    def disposable_reports(self, run, worktree):
        config = Config.model_validate(run["config"]).tests
        git = Git(worktree)
        tracked = set(git.call("ls-files", "-z").split("\0"))
        outputs = [contained(worktree, path) for path in config.output_paths]
        reports = set()
        for command in config.commands.values():
            if command.junit_report:
                contained(worktree, command.junit_report)
                for path in junit_paths(worktree, command.junit_report, outputs):
                    relative = path.relative_to(worktree).as_posix()
                    if relative in tracked:
                        raise ApplicationError("RUN_WORKTREE_DIRTY", "JUnit report is tracked")
                    reports.add(relative)
        changed = git.call("diff", "HEAD", "--name-only", "-z")
        untracked = set(
            filter(None, git.call("ls-files", "--others", "--exclude-standard", "-z").split("\0"))
        )
        if changed or untracked - reports:
            raise ApplicationError("RUN_WORKTREE_DIRTY", "worktree contains user changes")
        return [contained(worktree, name) for name in sorted(reports)]

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
                self.branch_target(run, worktree)
                reports = self.disposable_reports(run, worktree) if worktree.exists() else []
                if not run.get("deletion_pending"):
                    self.store.update(
                        run_id, deletion_pending=True, expected_version=expected_version
                    )
            # Preserve the row on any filesystem failure. A subsequent confirmed
            # request uses the latest version and repeats the same safe removal.
            try:
                retire_run_capacity(self.store.path, run)
                if worktree.exists():
                    for report in reports:
                        contained(worktree, report.relative_to(worktree).as_posix()).unlink(
                            missing_ok=True
                        )
                    Git(Path(run["repo"])).remove(worktree)  # Never force a dirty Worktree.
                target = self.branch_target(self.store.get(run_id), worktree)
                if target:
                    # Compare-and-delete also protects commits added after preflight.
                    with metadata(Path(run["repo"])):
                        Git(Path(run["repo"])).call(
                            "update-ref", "--no-deref", "-d", "refs/heads/" + run["branch"], target
                        )
                with self.store.transaction() as db:
                    self.check_idle(db, self.store.get(run_id), directory)
                    shutil.rmtree(directory)
                    self.delete_rows(db, run_id)
            except ApplicationError:
                raise
            except (OSError, PipelineError) as error:
                raise ApplicationError(
                    "RUN_DELETE_FAILED",
                    "file or branch removal failed; retry deletion with the latest version",
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
