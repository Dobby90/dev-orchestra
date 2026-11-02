"""Shared application service; independent of Typer and future HTTP adapters."""
import json
import subprocess
import sys
import uuid
from pathlib import Path
from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import digest, write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.store import Store, now


class RunService:
    def __init__(self, config: Config):
        self.config = config
        self.store = Store(config.storage.database)

    def create(self, repo: Path, spec: Path, base=None, max_fixes=None):
        config = self.config.model_copy(deep=True)
        if max_fixes is not None:
            config.workflow.max_fix_attempts = max_fixes
        config = Config.model_validate(config.model_dump())
        if not config.tests.commands:
            raise PipelineError("configure tests.commands with argv and junit_report before running")
        repo = repo.expanduser().resolve(strict=True)
        git = Git(repo)
        base = base or config.project.default_base_branch
        sha = git.baseline(base)
        spec_text = spec.expanduser().resolve(strict=True).read_text()
        if not spec_text.strip():
            raise PipelineError("PRD must not be empty")
        run_id = "run-" + uuid.uuid4().hex[:16]
        directory = config.storage.runs_directory / run_id
        worktree = config.storage.worktrees_directory / run_id
        root = Path(git.call("rev-parse", "--show-toplevel"))
        for path in [directory, worktree, config.storage.database]:
            if path.resolve().is_relative_to(root.resolve()):
                raise PipelineError("storage and worktrees must be outside the target repository")
        data = {"id": run_id, "repo": str(repo), "base": base, "base_sha": sha,
                "branch": f"{config.project.branch_prefix}/{run_id}", "worktree": str(worktree),
                "directory": str(directory), "spec": spec_text, "spec_hash": digest(spec_text.encode()),
                "config": config.model_dump(mode="json"), "iteration": 0, "next_role": "planner", "created_at": now()}
        write(directory / "spec.md", spec_text)
        write(directory / "config.json", data["config"])
        self.store.create(data)
        return run_id

    def start(self, run_id, detached=False):
        args = [sys.executable, "-m", "dev_orchestra.worker", str(self.store.path), run_id]
        directory = Path(self.store.get(run_id)["directory"])
        with (directory / "worker.log").open("a") as output:
            process = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        if not detached:
            try:
                process.wait()
            except KeyboardInterrupt:
                self.store.control(run_id, "pause")
                process.wait()
        return process.pid

    def resume(self, run_id, answers=None, max_fixes=None):
        run = self.store.get(run_id)
        with lock(Path(run["directory"]) / "execution.lock"):
            if run["state"] not in {"failed", "paused", "needs_input"}:
                raise PipelineError("run is not resumable")
            changes = {}
            if answers:
                changes["answers"] = answers.read_text()
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
            self.store.control(run_id, None)
            self.store.update(run_id, "queued", error=None, **changes)

    def cancel(self, run_id, pause=False):
        run = self.store.get(run_id)
        if run["state"] in {"completed", "cancelled"}:
            raise PipelineError("terminal run cannot be cancelled/paused")
        self.store.control(run_id, "pause" if pause else "cancel")
        try:
            with lock(Path(run["directory"]) / "execution.lock"):
                self.store.update(run_id, "paused" if pause else "cancelled")
        except PipelineError:
            pass  # Active runner acknowledges after its process group is terminated.

    def approve(self, run_id):
        run = self.store.get(run_id)
        with lock(Path(run["directory"]) / "execution.lock"):
            git = Git(Path(run["worktree"]), Config.model_validate(run["config"]).tests.output_paths)
            if run["state"] != "waiting_approval" or git.dirty() or git.sha() != run.get("final_sha"):
                raise PipelineError("approval requires unchanged verified HEAD and a clean worktree")
            if run.get("next_role") != "done" or not run["test_result"]["execution"]["passed"]:
                raise PipelineError("missing successful validation evidence")
            for step in self.store.steps(run_id):
                if step["status"] == "completed":
                    artifact = Path(step["data"]["artifact"])
                    if digest(artifact.read_bytes()) != step["data"]["hash"]:
                        raise PipelineError("approval evidence was modified")
            self.store.approve(run_id, git.sha(), digest(json.dumps(run["test_result"], sort_keys=True).encode()))

    def cleanup(self, run_id):
        run = self.store.get(run_id)
        with lock(Path(run["directory"]) / "execution.lock"):
            if run["state"] not in {"completed", "cancelled", "failed", "needs_input"}:
                raise PipelineError("active run cannot be cleaned up")
            Git(Path(run["repo"])).remove(Path(run["worktree"]))
