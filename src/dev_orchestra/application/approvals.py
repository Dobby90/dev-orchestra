"""Transport-independent approvals application service."""

import json
from pathlib import Path

from dev_orchestra.application.contracts import ApprovalCommand
from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.evidence import verified_candidate
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import lock


class ApprovalService:
    def __init__(self, store, refresh_report):
        self.store, self.refresh_report = store, refresh_report

    def approve_command(self, command: ApprovalCommand):
        return self.approve(command.run_id, command.expected_version, command.expected_sha)

    def approve(self, run_id, expected_version=None, expected_sha=None):
        run = self.store.get(run_id)
        with lock(Path(run["directory"]) / "execution.lock"):
            run = self.store.get(run_id)
            if expected_version is not None and run["version"] != expected_version:
                raise ApplicationError(
                    "VERSION_CONFLICT", "run version changed; refresh before approving"
                )
            if expected_sha is not None and run.get("final_sha") != expected_sha:
                raise ApplicationError(
                    "CANDIDATE_CHANGED", "candidate SHA changed; refresh before approving"
                )
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
