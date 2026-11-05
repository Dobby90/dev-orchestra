"""Shared application service; independent of Typer and future HTTP adapters."""

import uuid
from pathlib import Path

from dev_orchestra.application.approvals import ApprovalService
from dev_orchestra.application.contracts import CreateRun, RepositoryInfo
from dev_orchestra.application.events import EventService
from dev_orchestra.application.execution import ExecutionService
from dev_orchestra.application.handoff import repository_identity, verify_branch
from dev_orchestra.application.queries import RunQueryService
from dev_orchestra.application.recovery import RecoveryService
from dev_orchestra.config import Config
from dev_orchestra.domain.execution import PATCH_V1, WORKSPACE_V2, execution_contract
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.coordination import metadata
from dev_orchestra.infrastructure.evidence import checkpoints
from dev_orchestra.infrastructure.files import digest, write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.infrastructure.process import execution_scope
from dev_orchestra.infrastructure.process_owner import ensure_stopped
from dev_orchestra.infrastructure.store import Store, now


class RunService:
    def __init__(self, config: Config, *, _execution_contract=None):
        # The private override is for isolated contract tests only. Public activation
        # requires the backed-up schema boundary that older executables reject.
        self.execution_contract = (
            execution_contract({"execution_contract": _execution_contract})
            if _execution_contract is not None
            else None
        )
        self.config = config
        self.store = Store(config.storage.database)
        self.queries = RunQueryService(self.store)
        self.events = EventService(self.store)
        self.approvals = ApprovalService(self.store, self.refresh_report)
        self.recovery = RecoveryService(self.store, self.refresh_report)
        self.execution = ExecutionService(self.store, self.status)

    def default_execution_contract(self):
        with self.store.connection() as db:
            version = db.execute("SELECT version FROM schema_version").fetchone()[0]
        return WORKSPACE_V2 if version == 6 else PATCH_V1

    def create(self, repo: Path, spec: Path, base=None, max_fixes=None, test_scope="full"):
        command = CreateRun(
            repo=repo, spec=spec, base=base, max_fixes=max_fixes, test_scope=test_scope
        )
        return self.create_run(command)

    def create_run(self, command: CreateRun):
        data = self.prepare_run(command)
        self.store.create(data)
        return data["id"]

    def prepare_run(self, command: CreateRun, *, run_id=None, branch=None, resolve_settings=True):
        repo, spec, base, max_fixes = command.repo, command.spec, command.base, command.max_fixes
        from dev_orchestra.application.agent_settings import AgentSettingsService

        config = (
            AgentSettingsService(self.config).apply(self.config)
            if resolve_settings
            else self.config.model_copy(deep=True)
        )
        if max_fixes is not None:
            config.workflow.max_fix_attempts = max_fixes
        config = Config.model_validate(config.model_dump())
        if not config.tests.commands:
            raise PipelineError(
                "configure tests.commands with argv and junit_report before running"
            )
        repo = repo.expanduser().resolve(strict=True)
        git = Git(repo)
        base = base or config.project.default_base_branch
        sha = git.baseline(base)
        from dev_orchestra.application.validation import complete_run_validation

        config = complete_run_validation(
            config,
            RepositoryInfo(
                repository_path=repo,
                git_common_directory=git.common(),
                base_branch=base,
                base_sha=sha,
            ),
        )
        spec_text = spec.expanduser().resolve(strict=True).read_text()
        if not spec_text.strip():
            raise PipelineError("PRD must not be empty")
        run_id = run_id or "run-" + uuid.uuid4().hex[:16]
        directory = config.storage.runs_directory / run_id
        worktree = config.storage.worktrees_directory / run_id
        root = Path(git.call("rev-parse", "--show-toplevel"))
        for path in [directory, worktree, config.storage.database]:
            if path.resolve().is_relative_to(root.resolve()):
                raise PipelineError("storage and worktrees must be outside the target repository")
        data = {
            "repository_identity": repository_identity(repo),
            "test_scope": command.test_scope,
            "test_selection_contract": 1,
            "execution_contract": self.execution_contract or self.default_execution_contract(),
            "id": run_id,
            "repo": str(repo),
            "base": base,
            "base_sha": sha,
            "branch": branch or f"{config.project.branch_prefix}/{run_id}",
            "worktree": str(worktree),
            "directory": str(directory),
            "spec": spec_text,
            "spec_hash": digest(spec_text.encode()),
            "config": config.model_dump(mode="json"),
            "iteration": 0,
            "revision": 0,
            "plan_approval_required": True,
            "next_role": "planner",
            "created_at": now(),
        }
        with self.store.connection() as db:
            data["review_contract"] = (
                2 if db.execute("SELECT version FROM schema_version").fetchone()[0] >= 5 else 1
            )
        write(directory / "spec.md", spec_text)
        write(directory / "config.json", data["config"])
        return data

    def start(self, run_id, detached=False):
        return self.execution.start(run_id, detached)

    def resume_changes(self, run, answers=None, max_fixes=None):
        if run.get("worktree_removed"):
            raise PipelineError("worktree was cleaned; create a new Run")
        if run.get("cleanup_requested"):
            raise PipelineError("cleanup is in progress")
        if run["state"] in {"completed", "cancelled", "waiting_approval", "waiting_plan_approval"}:
            raise PipelineError("run is not resumable")
        if run.get("test_change_application") and answers and answers.strip():
            raise PipelineError(
                "finish validation of the applied test changes before adding instructions"
            )
        if run.get("test_change_proposal") and answers and answers.strip():
            raise PipelineError("discard the saved test change proposal before adding instructions")
        ensure_stopped(Path(run["directory"]))
        checkpoints(self.store, run)
        release_checkpoint = False
        if run.get("workspace_checkpoint"):
            from dev_orchestra.application.workspace_recovery import inspect_workspace

            session, _ = inspect_workspace(self.store, run)
            if answers and answers.strip():
                if session.data["commits"] or session.data["files"] != session.data["initial"]:
                    raise PipelineError(
                        "unfinished workspace requirements are bound to its checkpoint; preserve files and create a new Run for changed requirements"
                    )
                release_checkpoint = True
        changes = {"workspace_checkpoint": None} if release_checkpoint else {}
        if release_checkpoint:
            # Retire only the inspected, unedited attempt. Its files remain as
            # history; unrelated untrusted sessions must still block recovery.
            changes["workspace_released_steps"] = [
                *run.get("workspace_released_steps", []),
                session.data["step_id"],
            ]
        if answers is not None and answers.strip():
            previous = run.get("answers", "")
            changes["answers"] = "\n\n".join(part for part in [previous, answers] if part)
            changes["clarifications"] = [
                *run.get("clarifications", []),
                {
                    "role": run.get("next_role"),
                    "questions": run.get("questions", []),
                    "answer": answers,
                },
            ]
            changes["questions"] = []
            if run.get("next_role") == "done":
                changes.update(
                    next_role="planner",
                    revision=run.get("revision", 0) + 1,
                    plan=None,
                    planning_draft=None,
                    criteria=[],
                    plan_approval=None,
                    final_sha=None,
                    test_result=None,
                    review_result=None,
                )
        if (
            run.get("plan_approval_required")
            and run.get("next_role") == "developer"
            and (not run.get("plan_approval") or answers and answers.strip())
        ):
            changes.update(
                next_role="planner",
                revision=run.get("revision", 0) + 1,
                plan=None,
                criteria=[],
                plan_approval=None,
            )
        if max_fixes is not None:
            if max_fixes <= run["config"]["workflow"]["max_fix_attempts"]:
                raise PipelineError("new fix budget must increase the recorded limit")
            config = Config.model_validate(run["config"])
            config.workflow.max_fix_attempts = max_fixes
            changes["config"] = config.model_dump(mode="json")
            if run.get("next_role") == "reviewer":
                changes |= {"next_role": "fixer", "iteration": run["iteration"] + 1}
        if run.get("error") == "fix budget exhausted" and max_fixes is None:
            raise PipelineError("explicitly increase max-fix-attempts to resume an exhausted run")
        return changes

    def commit_resume(self, db, run, changes, expected_version):
        from dev_orchestra.application.queue import QueueService

        queue = QueueService(self.store)
        if (
            queue.enabled()
            and db.execute(
                "SELECT 1 FROM jobs WHERE run_id=? AND state IN ('queued','claimed','running')",
                (run["id"],),
            ).fetchone()
        ):
            raise PipelineError("previous Worker is still finalizing; retry after job completion")
        self.store.interrupt_steps(run["id"], "previous worker exited before checkpoint")
        self.store.control(run["id"], None, expected_version)
        self.store.update(run["id"], "queued", error=None, **changes)
        return queue.enqueue(run["id"], connection=db) if queue.enabled() else None

    def resume(self, run_id, answers=None, max_fixes=None, *, expected_version=None):
        from dev_orchestra.application.queue import QueueService

        queue = QueueService(self.store)
        if queue.enabled():
            queue.recover()
        run = self.store.get(run_id)
        with lock(Path(run["directory"]) / "execution.lock"):
            run = self.store.get(run_id)
            changes = self.resume_changes(run, answers.read_text() if answers else None, max_fixes)
            with self.store.transaction() as db:
                self.commit_resume(
                    db,
                    run,
                    changes,
                    run["version"] if expected_version is None else expected_version,
                )

    def decide_test_changes(self, run_id, proposal_hash, input_sha, approve, reason=""):
        from dev_orchestra.application.queue import QueueService
        from dev_orchestra.application.test_changes import TestChangeService

        queue = QueueService(self.store)
        if queue.enabled():
            queue.recover()
        run = self.store.get(run_id)
        with lock(Path(run["directory"]) / "execution.lock"):
            run = self.store.get(run_id)
            ensure_stopped(Path(run["directory"]))
            service = TestChangeService(self.store)
            proposal = service.verify(run, proposal_hash, input_sha, approve=approve)
            changes = service.decision_changes(run, proposal, approve, reason)
            with self.store.transaction() as db:
                self.commit_resume(db, run, changes, run["version"])
                self.store._event(
                    db,
                    run_id,
                    "test_changes.decided",
                    {"proposal_id": proposal["id"], "approved": approve},
                )
        self.refresh_report(run_id)

    def request_control(self, run_id, action, expected_version=None, connection=None):
        if connection is None:
            with self.store.transaction() as db:
                return self.request_control(run_id, action, expected_version, db)
        self.store.control(run_id, action, expected_version)
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone():
            connection.execute(
                "UPDATE jobs SET state='cancelled' WHERE run_id=? AND state='queued'", (run_id,)
            )

    def cancel(self, run_id, pause=False, *, expected_version=None):
        self.request_control(run_id, "pause" if pause else "cancel", expected_version)
        run = self.store.get(run_id)
        try:
            with lock(Path(run["directory"]) / "execution.lock"):
                try:
                    ensure_stopped(Path(run["directory"]))
                except PipelineError as error:
                    self.store.update(run_id, "needs_input", error=str(error))
                    self.refresh_report(run_id)
                    raise
                self.store.interrupt_steps(run_id, "cancelled after worker exit")
                self.store.update(run_id, "paused" if pause else "cancelled")
                self.refresh_report(run_id)
        except OwnershipError:
            pass

    def approve(self, run_id):
        return self.approvals.approve(run_id)

    def refresh_report(self, run_id):
        from dev_orchestra.orchestration.runner import Runner

        Runner(self.store, None, None).report(run_id)

    def delete(self, run_id, expected_version, *, confirm=False):
        from dev_orchestra.application.deletion import RunDeletionService

        RunDeletionService(self.store).delete(run_id, expected_version, confirm=confirm)

    def cleanup(self, run_id, expected_version=None, *, operation=None):
        run = self.store.get(run_id)
        directory = Path(run["directory"])
        with (
            lock(directory / "execution.lock"),
            execution_scope(directory),
            metadata(Path(run["repo"]), directory),
        ):
            run = self.store.get(run_id)
            # An operation may have committed removal before its caller crashed.
            applied = run.get("worktree_removed") and (
                operation is not None and run.get("cleanup_operation") == operation
            )
            if expected_version is not None and run["version"] != expected_version and not applied:
                raise PipelineError("concurrent state update")
            if run["state"] not in {"completed", "cancelled", "failed", "needs_input"}:
                raise PipelineError("active run cannot be cleaned up")
            ensure_stopped(directory)
            if run["state"] == "completed":
                verify_branch(run, run["final_sha"], worktree=Path(run["worktree"]).exists())
            from dev_orchestra.application.results import Results

            if Path(run["worktree"]).exists():
                if run.get("current_sha"):
                    Results(self.store).save_snapshot(run_id, run["current_sha"])
                # Already inside the short metadata lock: do not reacquire the
                # non-reentrant serialized Git.remove wrapper.
                Git(Path(run["repo"])).call("worktree", "remove", run["worktree"])
            with self.store.transaction() as db:
                if not run.get("worktree_removed") or run.get("cleanup_requested"):
                    self.store.update(
                        run_id,
                        worktree_removed=True,
                        cleanup_requested=False,
                        expected_version=run["version"],
                    )
                if operation is not None:
                    db.execute(
                        "UPDATE operations SET status='applied',applied_at=?,error=NULL WHERE id=? AND run_id=?",
                        (now(), operation, run_id),
                    )

    def _finish_idle_control(self, run):
        return self.recovery._finish_idle_control(run)

    def _reconcile_worker(self, run):
        return self.recovery._reconcile_worker(run)

    def status(self, run_id):
        return self.recovery.status(run_id)

    def report(self, run_id):
        run = self.store.get(run_id)
        self.refresh_report(run_id)
        path = Path(run["directory"]) / "final-report.md"
        if not path.exists():
            raise PipelineError("report is not available yet")
        return path.read_text()

    def log_paths(self, run_id, role=None):
        return self.events.log_paths(run_id, role)

    def log_chunks(self, run_id, cursor=None, role=None, limit=65536):
        return self.events.log_chunks(run_id, cursor, role, limit)
