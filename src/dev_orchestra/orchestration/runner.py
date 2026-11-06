"""Durable sequential state machine with SHA-bound checkpoints."""

import json
import logging
import os
from contextlib import nullcontext
from pathlib import Path

from dev_orchestra.agents.contracts import ResponseError, validate_role
from dev_orchestra.agents.feedback import source_context
from dev_orchestra.agents.policy import TestChangesRequired, apply_patch, validate_patch
from dev_orchestra.application.test_changes import TestChangeService
from dev_orchestra.application.workspace_recovery import WorkspaceBlocked
from dev_orchestra.config import Config
from dev_orchestra.domain.execution import WORKSPACE_V2, execution_contract, writes_workspace
from dev_orchestra.domain.models import AgentRequest, PipelineError, StopRequested
from dev_orchestra.infrastructure.commit_messages import describe_commit
from dev_orchestra.infrastructure.evidence import checkpoints, verified_candidate
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process import execution_scope, redact, sanitize
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
                "waiting_plan_approval",
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
                from dev_orchestra.infrastructure.coordination import execution_capacity

                with execution_capacity(self.store.path, run, stop):
                    ensure_stopped(directory)
                    if claim:
                        from dev_orchestra.application.queue import QueueService

                        QueueService(self.store).running(*claim)
                        self.validate_registered_repository(run)
                    if hasattr(self.tests, "check_policy"):
                        self.tests.check_policy(Config.model_validate(run["config"]).tests)
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
            except WorkspaceBlocked as error:
                self.store.update(run_id, "needs_input", error=redact(str(error)))
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
        execution_contract(run)
        if run["control"]:
            raise StopRequested(run["control"])
        config = Config.model_validate(run["config"])
        checkpoints(self.store, run)
        directory, worktree = Path(run["directory"]), Path(run["worktree"])
        original = Git(Path(run["repo"]))
        if run.get("workspace_checkpoint") and not worktree.exists():
            raise WorkspaceBlocked("recorded workspace is missing; refusing to recreate it")
        # Each repository mutation has an intent in the run record before it is attempted.
        if not worktree.exists():
            self.store.update(run_id, "preparing")
            original.create(worktree, run["branch"], run["base_sha"])
        git = Git(worktree, config.tests.output_paths)
        if git.call("branch", "--show-current") != run["branch"]:
            if run.get("workspace_checkpoint"):
                raise WorkspaceBlocked("recorded workspace branch changed; files preserved")
            raise PipelineError("worktree branch changed externally")
        resumed_workspace = None
        if run.get("workspace_checkpoint"):
            from dev_orchestra.application.workspace_recovery import recover_workspace

            try:
                from dev_orchestra.infrastructure.coordination import metadata

                with metadata(Path(run["repo"])):
                    resumed_workspace = recover_workspace(self.store, run)
                run = self.store.get(run_id)
            except (PipelineError, ValueError, OSError, KeyError) as error:
                self.store.update(
                    run_id,
                    "needs_input",
                    error="workspace recovery blocked; files preserved: " + redact(str(error)),
                )
                return
        if git.dirty() and not resumed_workspace:
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
            if role == "developer" and run.get("plan_approval_required"):
                from dev_orchestra.application.plan_approval import verify_development

                verify_development(self.store, run)
            if role == "done":
                candidate = git.sha()
                verified_candidate(self.store, run, candidate)
                from dev_orchestra.application.results import Results

                Results(self.store).save_snapshot(run_id, candidate)
                self.store.update(run_id, "reporting")
                self.report(run_id)
                if git.dirty() or git.sha() != candidate:
                    raise PipelineError("candidate changed after verification")
                verified_candidate(self.store, self.store.get(run_id), candidate)
                self.store.update(run_id, "waiting_approval", final_sha=candidate)
                return
            if hasattr(self.tests, "prepare"):
                try:
                    self.tests.prepare(
                        worktree,
                        config.tests,
                        directory / "preflight",
                        lambda: self.store.get(run_id)["control"],
                    )
                    if resumed_workspace:
                        resumed_workspace.guard()
                    elif git.dirty():
                        raise PipelineError(
                            "dependency preparation created untracked source files; changes preserved"
                        )
                except StopRequested:
                    raise
                except PipelineError as error:
                    self.store.update(run_id, "needs_input", error=redact(str(error)))
                    return
            sha = git.sha()
            logging.getLogger(f"devorchestra.{run_id}").info(
                "Starting %s iteration %s at %s", role, iteration, sha
            )
            self.store.update(run_id, STATES[role], next_role=role)
            completed = [
                s
                for s in self.store.steps(run_id)
                if s["role"] == role
                and s["iteration"] == iteration
                and s.get("revision", 0) == run.get("revision", 0)
                and s["status"] == "completed"
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
                instruction_hash = digest(
                    json.dumps(
                        {k: run.get(k) for k in ("answers", "clarifications", "directives")},
                        sort_keys=True,
                    ).encode()
                )[:16]
                repair_key = f"{run.get('revision', 0)}:{iteration}:{role}:{sha}:{instruction_hash}"
                failures = run.get("response_failures", {}).get(repair_key, 0)
                if not run.get("test_change_application") and not run.get("test_change_proposal"):
                    if (
                        failures > config.workflow.response_retries
                        or run.get("response_retry_inflight") == repair_key
                    ):
                        self.store.update(
                            run_id,
                            "needs_input",
                            error="response repair budget exhausted or interrupted; inspect before continuing",
                        )
                        return
                if resumed_workspace:
                    saved = resumed_workspace.data
                    step_id = saved["step_id"]
                    step = next(s for s in self.store.steps(run_id) if s["id"] == step_id)
                    attempt = step["attempt"]
                    step_dir = resumed_workspace.path.parent
                    sha = saved["input_sha"]
                else:
                    if execution_contract(run) == WORKSPACE_V2 and role in {
                        "developer",
                        "fixer",
                        "tester",
                    }:
                        for previous in self.store.steps(run_id):
                            if (
                                previous["role"] == role
                                and previous["iteration"] == iteration
                                and previous.get("revision", 0) == run.get("revision", 0)
                                and previous["status"] != "completed"
                                and not previous["data"].get("artifact")
                                and previous["id"] not in run.get("workspace_released_steps", [])
                            ):
                                old = (
                                    directory
                                    / "iterations"
                                    / f"{iteration:03}"
                                    / role
                                    / f"attempt-{previous['attempt']:03}"
                                    / "workspace-session.json"
                                )
                                if old.exists():
                                    raise WorkspaceBlocked(
                                        "legacy workspace checkpoint has no trusted snapshot; files preserved"
                                    )
                    step_id, attempt = self.store.start_step(
                        run_id,
                        role,
                        iteration,
                        (run.get("test_change_application") or {}).get("input_sha", sha),
                    )
                    step_dir = (
                        directory
                        / "iterations"
                        / f"{iteration:03}"
                        / role
                        / f"attempt-{attempt:03}"
                    )
                context = {
                    "execution_contract": execution_contract(run),
                    "prd": run["spec"],
                    "answers": run.get("answers", ""),
                    "clarifications": run.get("clarifications", []),
                    "directives": run.get("directives", []),
                    "review_contract": run.get("review_contract", 1),
                    "base_sha": run["base_sha"],
                    "test_paths": config.tests.write_paths,
                    "test_scope": run.get("test_scope", "full"),
                    "automatic_test_selection": run.get("test_selection_contract") == 2,
                    "test_commands": {k: v.model_dump() for k, v in config.tests.commands.items()},
                    "approved_test_changes": run.get("test_change_approvals", []),
                    "test_change_records": run.get("test_change_records", []),
                    "validation_policy": {
                        "immutable_for_run": True,
                        "change_target": "project validation settings (GUI) or tests in the CLI YAML config",
                        "change_effect": "new runs only; resume answers cannot change command policy",
                        "output_paths": config.tests.output_paths,
                    },
                }
                for key in ["plan", "criteria", "test_result", "review_result"]:
                    context[key] = run.get(key)
                if role == "planner":
                    context["planning_draft"] = run.get("planning_draft")
                if role == "reviewer":
                    context["diff"] = git.diff(run["base_sha"], sha)
                    context["candidate_sha"] = sha
                    if run.get("review_contract") == 2:
                        from dev_orchestra.application.reviews import Reviews

                        context["open_issues"] = Reviews(self.store).open(run_id)
                request = AgentRequest(
                    run_id=run_id,
                    role=role,
                    worktree=str(worktree),
                    iteration=iteration,
                    input_sha=sha,
                    context=context,
                )
                from dev_orchestra.application.test_selection import select_tests

                selection = (
                    select_tests(run, worktree, git.sha())
                    if role == "tester" and run.get("test_selection_contract")
                    else None
                )
                if selection:
                    request.context["test_selection"] = selection
                if role == "fixer" and run.get("review_contract") == 2:
                    from dev_orchestra.application.reviews import Reviews

                    request.context["open_issues"] = Reviews(self.store).open(run_id)
                if run.get("test_selection_contract") or execution_contract(run) == WORKSPACE_V2:
                    from dev_orchestra.agents.context import role_context

                    role_context(request)
                approval, record = None, None
                workspace, workspace_commits, workspace_execution = None, [], None
                if writes_workspace(request):
                    from dev_orchestra.infrastructure.workspace import WorkspaceSession

                    if not hasattr(self.agent, "run_workspace"):
                        raise PipelineError("agent adapter does not support workspace contract")
                    workspace = resumed_workspace or WorkspaceSession.create(
                        self.store, run, request, step_id, step_dir
                    )
                    resumed_workspace = None
                    workspace._test_runner = self.tests
                test_changes = TestChangeService(self.store)
                if run.get("test_change_application") and not workspace:
                    result, record, approval = test_changes.applied_result(run)
                elif run.get("test_change_proposal"):
                    if run["test_change_proposal"].get("approval"):
                        result, approval = test_changes.approved_result(run)
                    else:
                        _, result = test_changes.load(run)
                    if sha != run["test_change_proposal"]["input_sha"]:
                        raise PipelineError("saved test change input SHA changed")
                else:
                    feedback = run.get("response_feedback")
                    if feedback and feedback.get("key") == repair_key:
                        self.store.update(run_id, response_retry_inflight=repair_key)
                        request.context["response_feedback"] = feedback
                    request.context["response_contract"] = {
                        "input_sha": sha,
                        "criterion_ids": [c["id"] for c in run.get("criteria", [])],
                        "command_ids": list(config.tests.commands),
                        "patch_format": "Git unified diff with ---/+++ and @@ hunks; never *** markers",
                    }
                    if workspace:
                        request.context["response_contract"].pop("patch_format")
                        request.context["response_contract"]["edit_tool"] = "workspace.edit_file"
                        if role == "tester":
                            request.context["response_contract"]["run_tests_tool"] = (
                                "workspace.run_tests"
                            )
                            request.context["response_contract"]["command_ids"] = (
                                selection["included"] if selection else list(config.tests.commands)
                            )
                        else:
                            request.context["response_contract"]["checkpoint_tool"] = (
                                "workspace.checkpoint_commit"
                            )
                    if role == "tester" and run.get("test_selection_contract") == 2:
                        request.context["response_contract"].pop("command_ids", None)
                        request.context["response_contract"]["available_command_ids"] = list(
                            config.tests.commands
                        )
                    result = None
                    try:
                        if workspace:
                            result, workspace_sha, workspace_commits, workspace_execution = (
                                self.run_workspace_stage(
                                    request,
                                    step_dir,
                                    workspace,
                                    config,
                                )
                            )
                            if workspace_execution:
                                selection = workspace_execution["selection"]
                        elif (
                            role == "reviewer"
                            and run.get("test_selection_contract")
                            and hasattr(self.agent, "run_readonly")
                        ):
                            from dev_orchestra.infrastructure.workspace import ReviewEvidenceSession

                            readonly = ReviewEvidenceSession.create(
                                self.store, run, step_id, step_dir
                            )
                            request.context["test_evidence_id"] = readonly.data["evidence_id"]
                            result = self.agent.run_readonly(
                                request,
                                step_dir,
                                readonly,
                                lambda: self.store.get(run_id)["control"],
                            )
                        else:
                            result = self.agent.run(
                                request, step_dir, lambda: self.store.get(run_id)["control"]
                            )
                        if not workspace and (git.sha() != sha or git.dirty()):
                            raise PipelineError("read-only agent changed the worktree")
                        validate_role(role, result)
                        if not result.questions and not workspace:
                            if role == "tester":
                                self.validate_test_result(
                                    run,
                                    result,
                                    config,
                                    selection,
                                    legacy_scope=(
                                        run.get("test_scope") == "affected"
                                        and not workspace
                                        and run.get("test_selection_contract") != 2
                                    ),
                                )
                            try:
                                validate_patch(
                                    worktree, result.patch, role, config.tests.write_paths
                                )
                            except TestChangesRequired as protected:
                                test_changes.explanations(run, result, protected.paths)
                    except ResponseError as error:
                        if workspace:
                            raise PipelineError(
                                "workspace response invalid; confirmed commits and edits preserved"
                            ) from error
                        if git.sha() != sha or git.dirty():
                            raise PipelineError(
                                "candidate changed during response repair"
                            ) from error
                        feedback = {
                            "key": repair_key,
                            "input_sha": sha,
                            "error": redact(str(error))[:4000],
                            "current_files": sanitize(source_context(worktree, result.patch))
                            if result
                            else [],
                        }
                        with self.store.transaction():
                            self.store.finish_step(
                                step_id, sha, {"error": feedback["error"]}, status="failed"
                            )
                            self.store.update(
                                run_id,
                                response_feedback=feedback,
                                response_retry_inflight=None,
                                response_failures={
                                    **run.get("response_failures", {}),
                                    repair_key: failures + 1,
                                },
                            )
                        if failures >= config.workflow.response_retries:
                            self.store.update(
                                run_id,
                                "needs_input",
                                error="response repair budget exhausted: " + feedback["error"],
                            )
                            return
                        continue
                    self.store.update(run_id, response_feedback=None, response_retry_inflight=None)
                if not workspace and (git.sha() != sha or git.dirty()):
                    raise PipelineError("read-only agent changed the worktree")
                if result.questions:
                    changes = {"questions": result.questions}
                    if role == "planner":
                        changes["planning_draft"] = {
                            "plan": result.plan,
                            "criteria": [criterion.model_dump() for criterion in result.criteria],
                        }
                    self.store.update(run_id, "needs_input", **changes)
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
                if role == "tester":
                    self.validate_test_result(
                        run,
                        result,
                        config,
                        selection,
                        legacy_scope=(
                            run.get("test_scope") == "affected"
                            and not workspace
                            and run.get("test_selection_contract") != 2
                        ),
                    )
                if workspace:
                    workspace.guard()
                    out_sha = workspace_sha
                    patched_paths = list(
                        filter(
                            None, git.call("diff", "--name-only", "-z", sha, out_sha).split("\0")
                        )
                    )
                    if run.get("test_change_application"):
                        _, record, approval = test_changes.applied_result(run)
                    elif result.test_changes:
                        record = test_changes.prepare(
                            run | {"current_sha": workspace.data["input_sha"]},
                            result,
                            [change.file for change in result.test_changes],
                        )
                elif not run.get("test_change_application"):
                    try:
                        patched_paths = apply_patch(
                            worktree, result.patch, role, config.tests.write_paths
                        )
                    except TestChangesRequired as error:
                        record = test_changes.prepare(run, result, error.paths)
                        patched_paths = apply_patch(
                            worktree,
                            result.patch,
                            role,
                            config.tests.write_paths,
                            approved_patch_hash=approval["patch_hash"] if approval else None,
                            validated_patch_hash=record["patch_hash"],
                        )
                    out_sha = git.commit(
                        describe_commit(result.commit_message, result.summary, patched_paths)
                        + f"\n\nDevOrchestra-Run: {run_id}\nDevOrchestra-Role: {role}"
                        + f"\nDevOrchestra-Iteration: {iteration}\nDevOrchestra-Step: {step_id}"
                    )
                else:
                    out_sha, patched_paths = sha, []
                if patched_paths:
                    tracked = set(
                        git.call("ls-files", "--cached", "-z", "--", *patched_paths).split("\0")
                    )
                    if not set(patched_paths) <= tracked:
                        raise PipelineError("patch contains files absent from committed candidate")
                    git.call(
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--exit-code",
                        out_sha,
                        "--",
                        *patched_paths,
                    )
                if record and not run.get("test_change_application"):
                    application_path = step_dir / "applied-test-change.json"
                    application_hash = write(
                        application_path,
                        {
                            "result": sanitize(result.model_dump()),
                            "record": record,
                            "approval": approval,
                            "sha": out_sha,
                        },
                    )
                    with self.store.transaction() as db:
                        self.store.update(
                            run_id,
                            current_sha=out_sha,
                            test_change_proposal=None,
                            test_change_records=[*run.get("test_change_records", []), record],
                            test_change_application={
                                "input_sha": record["input_sha"],
                                "artifact": str(application_path.relative_to(directory)),
                                "hash": application_hash,
                            },
                        )
                        self.store._event(
                            db,
                            run_id,
                            "test_changes.applied",
                            {"record_id": record["id"], "sha": out_sha},
                        )
                else:
                    self.store.update(run_id, current_sha=out_sha)
                payload = result.model_dump() | {"sha": out_sha, "revision": run.get("revision", 0)}
                if workspace:
                    payload["workspace_commits"] = workspace_commits
                if approval:
                    payload["test_change_approval"] = approval
                if record:
                    payload["test_change_record"] = record
                if role == "tester":
                    if workspace_execution:
                        payload["execution"] = workspace_execution["execution"]
                        payload["test_evidence_reference"] = workspace.data["test_evidence"]
                    else:
                        from dev_orchestra.application.test_selection import selected_config

                        selection = (
                            select_tests(
                                run,
                                worktree,
                                out_sha,
                                plan=[item.model_dump() for item in result.test_plan]
                                if run.get("test_selection_contract") == 2
                                else None,
                            )
                            if run.get("test_selection_contract")
                            else None
                        )
                        self.validate_test_result(
                            run,
                            result,
                            config,
                            selection,
                            legacy_scope=(
                                run.get("test_scope") == "affected"
                                and not workspace
                                and run.get("test_selection_contract") != 2
                            ),
                        )
                        payload["execution"] = self.tests.run(
                            worktree,
                            selected_config(run, selection) if selection else config.tests,
                            step_dir,
                            lambda: self.store.get(run_id)["control"],
                        )
                    if selection:
                        payload["test_selection"] = selection
                        from dev_orchestra.application.test_selection import verify_selection

                        verify_selection(run, payload)
                    if git.dirty() or git.sha() != out_sha:
                        raise PipelineError(
                            "test execution modified tracked source or created untracked files"
                        )
                payload = sanitize(payload)
                artifact = step_dir / "result.json"
                from dev_orchestra.application.results import Results

                results = Results(self.store)
                failed_test = role == "tester" and payload.get("execution", {}).get("status") in {
                    "parse_error",
                    "unavailable",
                    "timeout",
                }
                atomic_result = (
                    bool(workspace)
                    or bool(record)
                    or results.enabled()
                    and (
                        role == "tester" or (role == "reviewer" and run.get("review_contract") == 2)
                    )
                )
                with self.store.transaction() if atomic_result else nullcontext() as db:
                    if role == "reviewer" and run.get("review_contract") == 2:
                        from dev_orchestra.application.reviews import Reviews
                        from dev_orchestra.domain.models import ReviewResultV2

                        if not isinstance(result, ReviewResultV2):
                            raise PipelineError("reviewer must return contract V2")
                        payload["issue_states"] = Reviews(self.store).apply(run, step_id, payload)
                    if role == "tester" and results.enabled():
                        for command in payload["execution"].get("commands", []):
                            if command.get("junit_path"):
                                command["junit_artifact_id"] = results.register(
                                    run_id, Path(command["junit_path"]), "junit", step_id
                                )
                            command.pop("junit_path", None)
                    checksum = write(artifact, payload)
                    evidence = {"artifact": str(artifact), "hash": checksum}
                    if failed_test:
                        self.store.finish_step(step_id, out_sha, evidence, status="failed")
                    else:
                        self.store.finish_step(step_id, out_sha, evidence)
                    if record:
                        self.store.update(run_id, test_change_application=None)
                    if workspace:
                        self.store.update(run_id, workspace_checkpoint=None)
                write(directory / FILES[role], payload)
                if failed_test:
                    failures = [
                        f"{c['command_id']} ({c.get('status', 'unavailable')}, "
                        f"exit code {c.get('exit_code')}): {c.get('error') or 'inspect command log'}"
                        for c in payload["execution"].get("commands", [])
                        if not c.get("passed")
                    ]
                    self.store.update(
                        run_id,
                        "needs_input",
                        error="test evidence unavailable; " + "; ".join(failures),
                        next_role="tester",
                    )
                    return
                self.store.update(run_id, current_sha=out_sha)
            self.advance(run_id, role, payload, config)
            if self.store.get(run_id)["state"] in {"needs_input", "waiting_plan_approval"}:
                return

    def run_workspace_stage(self, request, directory, session, config):
        from dev_orchestra.agents.contracts import ResponseError

        while True:
            session.reload()
            session.guard()
            data = session.data
            try:
                if data.get("response") is not None:
                    response = session.saved_response()
                else:
                    if data.get("repair_pending"):
                        if data.get("repair_calls", 0) >= config.workflow.response_retries:
                            raise WorkspaceBlocked(
                                "workspace response repair budget exhausted; checkpoint preserved"
                            )
                        data["repair_calls"] = data.get("repair_calls", 0) + 1
                        session.save()
                    if request.role == "tester":
                        request.context["test_selection"] = session.test_policy()
                        policy = request.context["test_selection"]
                        if policy.get("selection_hash") or policy.get("selector_version") != 2:
                            request.context["response_contract"]["command_ids"] = policy["included"]
                        else:
                            request.context["response_contract"].pop("command_ids", None)
                            request.context["response_contract"]["available_command_ids"] = list(
                                config.tests.commands
                            )
                    request.context["workspace_resume"] = {
                        "continuing": data.get("continuing", False),
                        "metadata_only": data.get("metadata_only", False),
                        "candidate_sha": session.git.sha(),
                        "completed_units": [c["summary"][:1000] for c in data["commits"][-5:]],
                        "preserved_files": {
                            name: value
                            for name, value in data["files"].items()
                            if value != data["initial"].get(name)
                        },
                        "remaining": data.get(
                            "response_error",
                            "Continue only the unfinished work on the preserved files.",
                        ),
                        "test_evidence": data.get("test_evidence"),
                    }
                    response = self.agent.run_workspace(
                        request,
                        directory,
                        session,
                        lambda: self.store.get(request.run_id)["control"],
                    )
                    session.capture_response(response)
                result, sha, commits = session.finish(response)
                validate_role(request.role, result)
                execution = None
                if request.role == "tester" and not result.questions:
                    execution = session.load_test_result(response)
                    self.validate_test_result(
                        self.store.get(request.run_id), result, config, execution["selection"]
                    )
                return result, sha, commits, execution
            except ResponseError as error:
                try:
                    session.reload()
                    session.guard()
                except PipelineError as boundary:
                    raise WorkspaceBlocked(str(boundary)) from boundary
                session.data.update(
                    response=None,
                    metadata_only=(
                        request.role != "tester"
                        or bool(session.data.get("test_evidence"))
                        and not session.changed_files()
                        and session.read_test_payload()["sha"] == session.git.sha()
                    ),
                    repair_pending=True,
                    continuing=True,
                    response_error=redact(str(error))[:4000],
                )
                session.save()
                # Completed work stays locked; a Tester without evidence can still run tests.
                continue
            except StopRequested:
                raise
            except PipelineError:
                try:
                    session.reload()
                    session.guard()
                except PipelineError as boundary:
                    raise WorkspaceBlocked(str(boundary)) from boundary
                raise

    @staticmethod
    def validate_test_result(run, result, config, selection=None, *, legacy_scope=False):
        if run.get("test_selection_contract") == 2 and not (selection or {}).get("selection_hash"):
            plan_ids = [item.command_id for item in result.test_plan]
            if (
                not plan_ids
                or len(plan_ids) != len(set(plan_ids))
                or not set(plan_ids) <= set(config.tests.commands)
                or any(not item.reason.strip() for item in result.test_plan)
            ):
                raise ResponseError(
                    "automatic tester requires a unique configured test_plan with reasons"
                )
            ids = {item.command_id for item in result.test_plan}
            ids |= {key for key, cmd in config.tests.commands.items() if cmd.kind == "check"}
            if config.tests.affected:
                ids |= set(config.tests.affected.required_commands)
                while True:
                    expanded = ids | {
                        key
                        for key, deps in config.tests.affected.dependencies.items()
                        if set(deps) & ids
                    }
                    if expanded == ids:
                        break
                    ids = expanded
        else:
            ids = set(selection["included"]) if selection else set(config.tests.commands)
        reported_ids = set(config.tests.commands) if legacy_scope else ids
        if set(result.command_ids) != reported_ids or len(result.command_ids) != len(reported_ids):
            raise ResponseError(
                f"tester command_ids mismatch: expected={sorted(reported_ids)}, received={result.command_ids}"
            )
        criteria = {c["id"] for c in run["criteria"]}
        test_ids = {k for k, v in config.tests.commands.items() if v.kind == "test" and k in ids}
        coverage = {c.criterion_id: c.command_ids for c in result.coverage}
        if (
            len(result.coverage) != len(criteria)
            or set(coverage) != criteria
            or any(not v or not set(v) <= ids or not set(v) & test_ids for v in coverage.values())
        ):
            raise ResponseError(
                "missing acceptance criterion test coverage: "
                f"missing={sorted(criteria - set(coverage))}, unexpected={sorted(set(coverage) - criteria)}, "
                f"expected={sorted(criteria)}, received={[c.criterion_id for c in result.coverage]}, "
                f"allowed_commands={sorted(ids)}, received_commands={coverage}; "
                "include each criterion exactly once with at least one configured test command"
            )
        if (
            selection
            and selection.get("selector_version") == 1
            and selection["actual_scope"] == "affected"
        ):
            mapping = config.tests.affected.criterion_commands
            if any(
                not set(commands) & set(mapping[key]) & test_ids
                for key, commands in coverage.items()
            ):
                raise ResponseError("coverage must reference the host criterion-to-test mapping")

    def advance(self, run_id, role, payload, config):
        run = self.store.get(run_id)
        directory = Path(run["directory"])
        if role == "planner":
            write(directory / "implementation-plan.md", payload["plan"])
            write(directory / "acceptance-criteria.json", payload["criteria"])
            self.store.update(
                run_id,
                state="waiting_plan_approval" if run.get("plan_approval_required") else None,
                next_role="developer",
                plan=payload["plan"],
                criteria=payload["criteria"],
                planning_draft=None,
                questions=[],
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
            ) or any(
                i["severity"] in config.review.blocking_severities and i["status"] == "unresolved"
                for i in payload.get("issue_states", [])
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
        self.store.get(run_id)
        # Keep the lock outside the removable run directory. A report that read
        # old state must never recreate files after a concurrent deletion.
        report_lock = self.store.path.parent / ".run-deletions" / (run_id + ".report.lock")
        with lock(report_lock, wait=True):
            self._report(run_id)

    def _report(self, run_id):
        run = self.store.get(run_id)
        if run.get("deletion_pending"):
            raise PipelineError("run deletion is pending; retry deletion")
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
                "final_sha",
                "state",
                "iteration",
                "revision",
                "error",
                "questions",
            ]
        }
        from dev_orchestra.application.reporting import result_markdown

        content = result_markdown(self.store, run, steps)
        content += "\n## 승인·인계 정보\n\n" + "\n".join(
            f"- **{k}**: {v}" for k, v in summary.items()
        )
        from dev_orchestra.application.handoff import handoff_markdown

        content += handoff_markdown(run)
        content += "\n\n리뷰 범위: Run 누적 변경과 직접 영향을 받는 코드·호출부·기존 미해결 이슈.\n"
        content += "\n\n## 테스트 범위\n\n요청 범위: " + run.get("test_scope", "full")
        selection = (run.get("test_result") or {}).get("test_selection")
        if selection:
            content += "\n\n실제 범위: " + selection["actual_scope"]
            content += "\n\n실행 명령: " + ", ".join(selection["included"])
            content += "\n\n미실행 명령: " + (", ".join(selection["excluded"]) or "없음")
            content += "\n\n" + "\n".join(selection["reasons"])
            content += "\n\n" + "\n".join(
                key + ": " + value for key, value in selection.get("command_reasons", {}).items()
            )
        if run.get("test_change_records"):
            content += "\n\n## 기존 테스트 변경\n\n최종 승인 전에 전체 Diff와 다음 변경 근거를 확인하세요.\n\n"
            for record in run["test_change_records"]:
                for change in record["changes"]:
                    content += f"- `{change['file']}` ({', '.join(change['criterion_ids'])}): {change['reason']}\n"
        content += "\n\n## 부록: 원문 요구사항\n\n" + run["spec"]
        if run.get("directives"):
            content += "\n\n## 부록: 사용자 수정 요청\n\n" + "\n\n".join(run["directives"])
        content += "\n\n## 부록: 구현·검증 이력\n\n"
        for step in steps:
            content += f"- {step['role']} iteration={step['iteration']} attempt={step['attempt']} {step['status']} SHA={step['output_sha']}\n"
            from dev_orchestra.application.progress import role_progress

            progress = role_progress(self.store, run, step)
            for commit in progress["commits"]:
                content += f"  - 작업 단위 커밋 `{commit['sha']}`: {commit['summary']} (저장 완료, 검증 통과와 별도)\n"
            if progress["checkpoint_sha"]:
                content += f"  - 마지막 복구 지점 `{progress['checkpoint_sha']}` · 보존 변경 {progress['preserved_changes']}개 · 재개 방식 {progress['recovery']}\n"
        content += (
            "\n## 부록: 원본 테스트·리뷰 결과\n\n```json\n"
            + json.dumps(
                {"tests": run.get("test_result"), "review": run.get("review_result")},
                indent=2,
                ensure_ascii=False,
            )
            + "\n```\n"
        )
        content += (
            "\n\n자동 리뷰는 결함 부재를 보장하지 않습니다. Push·Merge는 자동 수행하지 않습니다.\n"
        )
        content = redact(content)
        write(Path(run["directory"]) / "metadata.json", summary)
        write(Path(run["directory"]) / "final-report.md", content)
        from dev_orchestra.application.results import Results

        Results(self.store).save_report(run_id, content, run["version"])
