"""Durable sequential state machine with SHA-bound checkpoints."""

import json
import logging
import os
from pathlib import Path

from dev_orchestra.agents.policy import apply_patch
from dev_orchestra.config import Config
from dev_orchestra.domain.models import AgentRequest, PipelineError, StopRequested
from dev_orchestra.infrastructure.evidence import checkpoints, verified_candidate
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process import execution_scope, redact
from dev_orchestra.infrastructure.process_owner import ensure_stopped, identity

STATES = {
    "planner": "planning",
    "developer": "developing",
    "tester": "testing",
    "reviewer": "reviewing",
    "fixer": "fixing",
}
FILES = {
    "planner": "planning-result.json",
    "developer": "development-result.json",
    "tester": "test-result.json",
    "reviewer": "review-result.json",
    "fixer": "fix-result.json",
}


class Runner:
    def __init__(self, store, agent, tests):
        self.store, self.agent, self.tests = store, agent, tests

    def execute(self, run_id, claim=None):
        run = self.store.get(run_id)
        directory = Path(run["directory"])
        with lock(directory / "execution.lock"), execution_scope(directory):
            run = self.store.get(run_id)
            if run["state"] in {
                "completed",
                "cancelled",
                "waiting_approval",
                "needs_input",
                "paused",
            }:
                return
            if claim:
                from dev_orchestra.application.queue import QueueService

                QueueService(self.store).enter(*claim, run_id=run_id)
            worker = identity(os.getpid()) | {"finished": False}
            write(directory / "worker.json", worker)
            logger = logging.getLogger(f"devorchestra.{run_id}")
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = logging.FileHandler(directory / "workflow.log")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logger.addHandler(handler)

            def stop():
                return self.store.get(run_id)["control"]

            try:
                with (
                    lock(self.store.path.with_suffix(".worker.lock"), wait=True, stop=stop),
                    lock(
                        Git(Path(run["repo"])).common() / "devorchestra.lock", wait=True, stop=stop
                    ),
                ):
                    ensure_stopped(directory)
                    if claim:
                        from dev_orchestra.application.queue import QueueService

                        QueueService(self.store).running(*claim)
                        self.validate_registered_repository(run)
                    if hasattr(self.agent, "check"):
                        self.agent.check(stop=stop)
                    if hasattr(self.tests, "check"):
                        self.tests.check(Path(run["repo"]), stop=stop)
                    self._execute(run_id)
            except StopRequested as error:
                try:
                    ensure_stopped(directory)
                except PipelineError as cleanup_error:
                    self.store.update(run_id, "needs_input", error=str(cleanup_error))
                else:
                    self.store.update(run_id, "cancelled" if str(error) == "cancel" else "paused")
            except Exception as error:
                self.store.update(run_id, "failed", error=redact(str(error)))
            finally:
                try:
                    ensure_stopped(directory)
                except PipelineError as error:
                    self.store.update(run_id, "needs_input", error=str(error))
                self.store.interrupt_steps(run_id, "stage interrupted before completion")
                self.report(run_id)
                logger.removeHandler(handler)
                handler.close()
                write(directory / "worker.json", worker | {"finished": True})

    def validate_registered_repository(self, run):
        with self.store.connection() as db:
            project = db.execute(
                "SELECT p.* FROM projects p JOIN runs r ON r.project_id=p.id WHERE r.id=?",
                (run["id"],),
            ).fetchone()
        if project:
            from dev_orchestra.application.projects import ProjectService

            info = ProjectService([Path(project["repository_path"]).parent]).validate_repository(
                Path(project["repository_path"]), run["base"]
            )
            stat = info.git_common_directory.stat()
            if run["repo"] != str(info.repository_path) or json.loads(project["identity"]) != {
                "device": stat.st_dev,
                "inode": stat.st_ino,
            }:
                raise PipelineError("registered repository identity changed")

    def _execute(self, run_id):
        run = self.store.get(run_id)
        if run["control"]:
            raise StopRequested(run["control"])
        config = Config.model_validate(run["config"])
        checkpoints(self.store, run)
        directory, worktree = Path(run["directory"]), Path(run["worktree"])
        original = Git(Path(run["repo"]))
        # Each repository mutation has an intent in the run record before it is attempted.
        if not worktree.exists():
            self.store.update(run_id, "preparing")
            original.create(worktree, run["branch"], run["base_sha"])
        git = Git(worktree, config.tests.output_paths)
        if git.call("branch", "--show-current") != run["branch"]:
            raise PipelineError("worktree branch changed externally")
        if git.dirty():
            self.store.update(
                run_id,
                "needs_input",
                error="uncommitted changes preserved; inspect before resuming",
            )
            return
        if run.get("current_sha") and git.sha() != run["current_sha"]:
            # A crash after host commit but before DB acknowledgement requires manual inspection.
            self.store.update(
                run_id, "needs_input", error="HEAD differs from checkpoint; changes preserved"
            )
            return
        self.store.update(run_id, current_sha=git.sha())
        while True:
            run = self.store.get(run_id)
            control = run["control"]
            if control:
                raise StopRequested(control)
            iteration = run.get("iteration", 0)
            role = run.get("next_role", "planner")
            if role == "done":
                candidate = git.sha()
                verified_candidate(self.store, run, candidate)
                self.store.update(run_id, "reporting")
                self.report(run_id)
                if git.dirty() or git.sha() != candidate:
                    raise PipelineError("candidate changed after verification")
                verified_candidate(self.store, self.store.get(run_id), candidate)
                self.store.update(run_id, "waiting_approval", final_sha=candidate)
                return
            sha = git.sha()
            logging.getLogger(f"devorchestra.{run_id}").info(
                "Starting %s iteration %s at %s", role, iteration, sha
            )
            self.store.update(run_id, STATES[role], next_role=role)
            completed = [
                s
                for s in self.store.steps(run_id)
                if s["role"] == role and s["iteration"] == iteration and s["status"] == "completed"
            ]
            if completed:
                step = completed[-1]
                if step["output_sha"] != sha:
                    raise PipelineError("completed checkpoint SHA changed")
                artifact = Path(step["data"]["artifact"])
                if not artifact.exists() or digest(artifact.read_bytes()) != step["data"]["hash"]:
                    raise PipelineError("checkpoint artifact checksum mismatch")
                payload = json.loads(artifact.read_text())
            else:
                step_id, attempt = self.store.start_step(run_id, role, iteration, sha)
                step_dir = (
                    directory / "iterations" / f"{iteration:03}" / role / f"attempt-{attempt:03}"
                )
                context = {
                    "prd": run["spec"],
                    "answers": run.get("answers", ""),
                    "base_sha": run["base_sha"],
                    "test_paths": config.tests.write_paths,
                    "test_commands": {k: v.model_dump() for k, v in config.tests.commands.items()},
                }
                for key in ["plan", "criteria", "test_result", "review_result"]:
                    context[key] = run.get(key)
                if role == "reviewer":
                    context["diff"] = git.diff(run["base_sha"], sha)
                    context["candidate_sha"] = sha
                request = AgentRequest(
                    run_id=run_id,
                    role=role,
                    worktree=str(worktree),
                    iteration=iteration,
                    input_sha=sha,
                    context=context,
                )
                result = self.agent.run(
                    request, step_dir, lambda: self.store.get(run_id)["control"]
                )
                if git.sha() != sha or git.dirty():
                    raise PipelineError("read-only agent changed the worktree")
                if result.questions:
                    self.store.update(run_id, "needs_input", questions=result.questions)
                    return
                if role == "planner":
                    if (
                        not result.plan
                        or not result.criteria
                        or len({c.id for c in result.criteria}) != len(result.criteria)
                    ):
                        raise PipelineError("plan and unique acceptance criteria are required")
                    if result.patch:
                        raise PipelineError("planner returned a source patch")
                if role == "reviewer":
                    if result.patch:
                        raise PipelineError("reviewer returned a source patch")
                    for issue in result.issues:
                        path = contained(worktree, issue.file)
                        if not path.is_file() or issue.line > len(path.read_text().splitlines()):
                            raise PipelineError("review finding has invalid source location")
                        if not issue.evidence.strip():
                            raise PipelineError("review finding requires evidence")
                apply_patch(worktree, result.patch, role, config.tests.write_paths)
                out_sha = git.commit(f"자동 개발: {run_id} {role} 회차 {iteration} 단계 {step_id}")
                self.store.update(run_id, current_sha=out_sha)
                payload = result.model_dump() | {"sha": out_sha}
                if role == "tester":
                    ids = set(config.tests.commands)
                    if set(result.command_ids) != ids:
                        raise PipelineError("tester must select all configured test commands")
                    criteria = {c["id"] for c in run["criteria"]}
                    coverage = {c.criterion_id: c.command_ids for c in result.coverage}
                    if set(coverage) != criteria or any(
                        not v or not set(v) <= ids for v in coverage.values()
                    ):
                        raise PipelineError("missing acceptance criterion test coverage")
                    payload["execution"] = self.tests.run(
                        worktree, config.tests, step_dir, lambda: self.store.get(run_id)["control"]
                    )
                    if git.dirty() or git.sha() != out_sha:
                        raise PipelineError(
                            "test execution modified tracked source or created untracked files"
                        )
                from dev_orchestra.infrastructure.process import sanitize

                payload = sanitize(payload)
                artifact = step_dir / "result.json"
                checksum = write(artifact, payload)
                write(directory / FILES[role], payload)
                self.store.finish_step(
                    step_id, out_sha, {"artifact": str(artifact), "hash": checksum}
                )
                self.store.update(run_id, current_sha=out_sha)
            self.advance(run_id, role, payload, config)
            if self.store.get(run_id)["state"] == "needs_input":
                return

    def advance(self, run_id, role, payload, config):
        run = self.store.get(run_id)
        directory = Path(run["directory"])
        if role == "planner":
            write(directory / "implementation-plan.md", payload["plan"])
            write(directory / "acceptance-criteria.json", payload["criteria"])
            self.store.update(
                run_id, next_role="developer", plan=payload["plan"], criteria=payload["criteria"]
            )
        elif role in {"developer", "fixer"}:
            self.store.update(run_id, next_role="tester")
        elif role == "tester":
            self.store.update(run_id, next_role="reviewer", test_result=payload)
        elif role == "reviewer":
            review_md = (
                "# Code review\n\n"
                + payload["summary"]
                + "\n\n"
                + "\n".join(
                    f"- {i['severity']}: {i['file']}:{i['line']} {i['description']} — {i['evidence']}"
                    for i in payload["issues"]
                )
            )
            write(directory / "review-result.md", review_md)
            blocking = any(
                i["severity"] in config.review.blocking_severities for i in payload["issues"]
            )
            passed = run["test_result"]["execution"]["passed"] and not blocking
            changes = {"review_result": payload}
            if passed:
                changes["next_role"] = "done"
            elif run.get("iteration", 0) >= config.workflow.max_fix_attempts:
                self.store.update(run_id, "needs_input", error="fix budget exhausted", **changes)
                return
            else:
                changes |= {"iteration": run.get("iteration", 0) + 1, "next_role": "fixer"}
            self.store.update(run_id, **changes)

    def report(self, run_id):
        run = self.store.get(run_id)
        steps = self.store.steps(run_id)
        summary = {
            k: run.get(k)
            for k in [
                "id",
                "repo",
                "branch",
                "base",
                "base_sha",
                "current_sha",
                "state",
                "iteration",
                "error",
                "questions",
            ]
        }
        content = "# DevOrchestra report\n\n" + "\n".join(
            f"- **{k}**: {v}" for k, v in summary.items()
        )
        content += "\n\n## Requirements\n\n" + run["spec"]
        content += "\n\n## Implementation and verification history\n\n"
        for step in steps:
            content += f"- {step['role']} iteration={step['iteration']} attempt={step['attempt']} {step['status']} SHA={step['output_sha']}\n"
        content += (
            "\n## Test and review results\n\n```json\n"
            + json.dumps(
                {"tests": run.get("test_result"), "review": run.get("review_result")},
                indent=2,
                ensure_ascii=False,
            )
            + "\n```\n"
        )
        if Path(run["worktree"]).exists():
            try:
                content += "\n## Changed files\n\n" + Git(Path(run["worktree"])).call(
                    "diff", "--name-only", f"{run['base_sha']}...HEAD"
                )
            except PipelineError:
                pass
        content += "\n\n## Remaining risks\n\nReview findings and missing/failed verification above remain actionable. Automatic review cannot prove absence of defects.\n\nHuman approval is required; no push or merge is performed.\n"
        content = redact(content)
        write(Path(run["directory"]) / "metadata.json", summary)
        write(Path(run["directory"]) / "final-report.md", content)
