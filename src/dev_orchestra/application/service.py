"""Shared application service; independent of Typer and future HTTP adapters."""

import json
import subprocess
import sys
import uuid
from pathlib import Path

from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.evidence import checkpoints, verified_candidate
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
            raise PipelineError(
                "configure tests.commands with argv and junit_report before running"
            )
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
        data = {
            "id": run_id,
            "repo": str(repo),
            "base": base,
            "base_sha": sha,
            "branch": f"{config.project.branch_prefix}/{run_id}",
            "worktree": str(worktree),
            "directory": str(directory),
            "spec": spec_text,
            "spec_hash": digest(spec_text.encode()),
            "config": config.model_dump(mode="json"),
            "iteration": 0,
            "next_role": "planner",
            "created_at": now(),
        }
        write(directory / "spec.md", spec_text)
        write(directory / "config.json", data["config"])
        self.store.create(data)
        return run_id

    def start(self, run_id, detached=False):
        args = [sys.executable, "-m", "dev_orchestra.worker", str(self.store.path), run_id]
        directory = Path(self.store.get(run_id)["directory"])
        with (directory / "worker.log").open("a") as output:
            process = subprocess.Popen(
                args, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
            )
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
            run = self.store.get(run_id)
            if run["state"] in {"completed", "cancelled", "waiting_approval"}:
                raise PipelineError("run is not resumable")
            import os

            for marker in Path(run["directory"]).glob("iterations/*/*/*/*.process.json"):
                record = json.loads(marker.read_text())
                if not record["finished"]:
                    try:
                        os.kill(record["pid"], 0)
                    except ProcessLookupError:
                        continue
                    raise PipelineError("orphan process still alive; inspect before resuming")
            checkpoints(self.store, run)
            self.store.interrupt_steps(run_id, "previous worker exited before checkpoint")
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
                raise PipelineError(
                    "explicitly increase max-fix-attempts to resume an exhausted run"
                )
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
            run = self.store.get(run_id)
            git = Git(
                Path(run["worktree"]), Config.model_validate(run["config"]).tests.output_paths
            )
            if (
                run["state"] != "waiting_approval"
                or git.dirty()
                or git.sha() != run.get("final_sha")
            ):
                raise PipelineError(
                    "approval requires unchanged verified HEAD and a clean worktree"
                )
            if run.get("next_role") != "done" or not run["test_result"]["execution"]["passed"]:
                raise PipelineError("missing successful validation evidence")
            sha = git.sha()
            verified_candidate(self.store, run, sha)
            if git.sha() != sha or git.dirty():
                raise PipelineError("candidate changed during approval")
            self.store.approve(
                run_id, sha, digest(json.dumps(run["test_result"], sort_keys=True).encode())
            )

        self.refresh_report(run_id)

    def refresh_report(self, run_id):
        from dev_orchestra.orchestration.runner import Runner

        Runner(self.store, None, None).report(run_id)

    def cleanup(self, run_id):
        run = self.store.get(run_id)
        with lock(Path(run["directory"]) / "execution.lock"):
            run = self.store.get(run_id)
            if run["state"] not in {"completed", "cancelled", "failed", "needs_input"}:
                raise PipelineError("active run cannot be cleaned up")
            Git(Path(run["repo"])).remove(Path(run["worktree"]))

    def status(self, run_id):
        run = self.store.get(run_id)
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

    def report(self, run_id):
        run = self.store.get(run_id)
        self.refresh_report(run_id)
        path = Path(run["directory"]) / "final-report.md"
        if not path.exists():
            raise PipelineError("report is not available yet")
        return path.read_text()

    def log_paths(self, run_id, role=None):
        if role is not None and role not in {"planner", "developer", "tester", "reviewer", "fixer"}:
            raise PipelineError("invalid role filter")
        directory = Path(self.store.get(run_id)["directory"])
        paths = sorted(directory.glob("iterations/*/*/*/*.log"))
        if role is None and (directory / "workflow.log").exists():
            paths.insert(0, directory / "workflow.log")
        return [p for p in paths if role is None or p.parent.parent.name == role]

    def log_chunks(self, run_id, cursor=None, role=None, limit=65536):
        """Replay durable file logs using per-attempt paths and byte offsets."""
        if not 1 <= limit <= 1048576:
            raise PipelineError("invalid log chunk limit")
        run = self.store.get(run_id)
        root = Path(run["directory"])
        cursor = dict(cursor or {})
        chunks = []
        for path in self.log_paths(run_id, role):
            key = str(path.relative_to(root))
            offset = cursor.get(key, 0)
            if not isinstance(offset, int) or offset < 0 or offset > path.stat().st_size:
                raise PipelineError("invalid log cursor")
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(limit)
                # Finish a UTF-8 code point so byte cursors never lose Korean text.
                if data:
                    import codecs

                    decoder = codecs.getincrementaldecoder("utf-8")("replace")
                    decoder.decode(data, final=False)
                    while decoder.getstate()[0]:
                        extra = handle.read(1)
                        if not extra:
                            break
                        data += extra
                        decoder.decode(extra, final=False)
            if data:
                chunks.append(
                    {
                        "run_id": run_id,
                        "log_id": key,
                        "offset": offset,
                        "text": data.decode("utf-8", errors="replace"),
                    }
                )
                cursor[key] = offset + len(data)
        return {"schema_version": 1, "chunks": chunks, "cursor": cursor}
