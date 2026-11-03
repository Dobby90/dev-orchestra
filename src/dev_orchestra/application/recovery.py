"""Transport-independent recovery application service."""

import json
from pathlib import Path

from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.infrastructure.process_owner import alive, ensure_stopped


class RecoveryService:
    def __init__(self, store, refresh_report):
        self.store, self.refresh_report = store, refresh_report

    def _finish_idle_control(self, run):
        if not run["control"] or run["state"] in {"completed", "cancelled"}:
            return run
        if run["control"] == "pause" and run["state"] == "paused":
            return run
        directory = Path(run["directory"])
        try:
            with lock(directory / "execution.lock"):
                run = self.store.get(run["id"])
                if not run["control"] or run["state"] in {"completed", "cancelled"}:
                    return run
                try:
                    ensure_stopped(directory)
                except PipelineError as error:
                    if run.get("error") != str(error):
                        self.store.update(run["id"], "needs_input", error=str(error))
                else:
                    self.store.interrupt_steps(run["id"], "idle control acknowledged")
                    self.store.update(
                        run["id"], "cancelled" if run["control"] == "cancel" else "paused"
                    )
                self.refresh_report(run["id"])
                return self.store.get(run["id"])
        except OwnershipError:
            return self.store.get(run["id"])

    def _reconcile_worker(self, run):
        with self.store.connection() as db:
            queued_execution = db.execute("SELECT version FROM schema_version").fetchone()[0] in {
                4,
                5,
            }
        if queued_execution:
            from dev_orchestra.application.queue import QueueService

            QueueService(self.store).recover()
            run = self.store.get(run["id"])
            with self.store.connection() as db:
                if db.execute(
                    "SELECT 1 FROM jobs WHERE run_id=? AND state IN ('queued','claimed','running')",
                    (run["id"],),
                ).fetchone():
                    # Durable job ownership supersedes a previous attempt's launch marker.
                    return run
        active = {
            "queued",
            "preparing",
            "planning",
            "developing",
            "testing",
            "reviewing",
            "fixing",
            "reporting",
        }
        if run["state"] not in active:
            return run
        if run["state"] == "queued":
            with self.store.connection() as db:
                if (
                    db.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone()
                    and db.execute(
                        "SELECT 1 FROM jobs WHERE run_id=? AND state='queued'", (run["id"],)
                    ).fetchone()
                ):
                    return run
        directory = Path(run["directory"])
        try:
            with lock(directory / "execution.lock"):
                run = self.store.get(run["id"])
                if run["state"] not in active:
                    return run
                launch_path, worker_path = directory / "launch.json", directory / "worker.json"
                launch = json.loads(launch_path.read_text()) if launch_path.exists() else None
                worker = json.loads(worker_path.read_text()) if worker_path.exists() else None
                if (
                    launch
                    and alive(launch)
                    and (
                        not worker
                        or any(launch.get(k) != worker.get(k) for k in ["pid", "create_time"])
                    )
                ):
                    return run  # The newly spawned Worker has not claimed the run yet.
                if not launch and not worker and run["state"] == "queued":
                    return run  # A run may be created without starting it immediately.
                self.store.interrupt_steps(run["id"], "Worker ownership lost")
                self.store.update(
                    run["id"],
                    "failed",
                    expected_version=run["version"],
                    error="Worker ownership lost; inspect or resume the preserved checkpoint",
                )
                self.refresh_report(run["id"])
                return self.store.get(run["id"])
        except OwnershipError:
            return self.store.get(run["id"])

    def status(self, run_id):
        if self.store.get(run_id).get("deletion_pending"):
            return self.store.get(run_id)
        run = self._reconcile_worker(self._finish_idle_control(self.store.get(run_id)))
        if run["state"] == "completed":
            branch_sha = Git(Path(run["repo"])).call("rev-parse", "refs/heads/" + run["branch"])
            dirty = False
            if Path(run["worktree"]).exists():
                dirty = bool(
                    Git(
                        Path(run["worktree"]),
                        Config.model_validate(run["config"]).tests.output_paths,
                    ).dirty()
                )
            if branch_sha != run.get("final_sha") or dirty:
                self.store.update(
                    run_id,
                    "needs_input",
                    error="approved result changed; historical approval retained",
                    expected_version=run["version"],
                )
                self.refresh_report(run_id)
                run = self.store.get(run_id)
        return run
