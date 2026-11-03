"""Exact-patch approval for requirement-driven edits to protected tests."""

import json
import uuid
from pathlib import Path

from dev_orchestra.agents.policy import TestChangesRequired, validate_patch
from dev_orchestra.config import Config
from dev_orchestra.domain.models import AgentResult, PipelineError
from dev_orchestra.infrastructure.evidence import checkpoints
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.process import sanitize
from dev_orchestra.infrastructure.store import now


def checksum(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False).encode())


def task_hash(run):
    return checksum(
        {k: run.get(k) for k in ["spec", "plan", "answers", "clarifications", "directives"]}
    )


class TestChangeService:
    def __init__(self, store):
        self.store = store

    def propose(self, run, result, paths, step_dir):
        changes = [c.model_dump() for c in result.test_changes]
        criteria = {c["id"] for c in run["criteria"]}
        if (
            {c["file"] for c in changes} != set(paths)
            or len(changes) != len(paths)
            or any(
                not c["reason"].strip() or not set(c["criterion_ids"]) <= criteria for c in changes
            )
        ):
            raise PipelineError(
                "protected test edits require test_changes with a reason and valid criterion IDs "
                "for every existing test file: " + ", ".join(paths)
            )
        payload = sanitize(result.model_dump())
        if payload["patch"] != result.patch:
            raise PipelineError("protected test patch contains redacted data")
        artifact = step_dir / "test-change-result.json"
        result_hash = write(artifact, payload)
        proposal = {
            "id": "test-change-" + uuid.uuid4().hex,
            "run_id": run["id"],
            "role": run["next_role"],
            "iteration": run.get("iteration", 0),
            "revision": run.get("revision", 0),
            "input_sha": run["current_sha"],
            "patch_hash": digest(result.patch.encode()),
            "result_hash": result_hash,
            "artifact": str(artifact.relative_to(Path(run["directory"]))),
            "policy_hash": checksum(run["config"]["tests"]),
            "criteria_hash": checksum(run["criteria"]),
            "task_hash": task_hash(run),
            "changes": payload["test_changes"],
            "created_at": now(),
        }
        proposal["proposal_hash"] = checksum(proposal)
        with self.store.transaction() as db:
            self.store.update(
                run["id"],
                "needs_input",
                test_change_proposal=proposal,
                error="기존 테스트 변경 승인 필요: " + ", ".join(paths),
                questions=[],
            )
            self.store._event(
                db, run["id"], "test_changes.proposed", {"proposal_id": proposal["id"]}
            )

    def load(self, run):
        proposal = run.get("test_change_proposal")
        if not proposal:
            raise PipelineError("no protected test change proposal")
        binding = {k: v for k, v in proposal.items() if k not in {"proposal_hash", "approval"}}
        if checksum(binding) != proposal["proposal_hash"]:
            raise PipelineError("test change proposal checksum mismatch")
        if any(
            proposal[key] != value
            for key, value in {
                "run_id": run["id"],
                "role": run["next_role"],
                "iteration": run.get("iteration", 0),
                "revision": run.get("revision", 0),
                "input_sha": run["current_sha"],
                "policy_hash": checksum(run["config"]["tests"]),
                "criteria_hash": checksum(run["criteria"]),
                "task_hash": task_hash(run),
            }.items()
        ):
            raise PipelineError("test change proposal no longer matches run context")
        artifact = contained(Path(run["directory"]), proposal["artifact"])
        raw = artifact.read_bytes()
        if digest(raw) != proposal["result_hash"]:
            raise PipelineError("test change result checksum mismatch")
        result = AgentResult.model_validate(json.loads(raw))
        if digest(result.patch.encode()) != proposal["patch_hash"]:
            raise PipelineError("test change patch checksum mismatch")
        return proposal, result

    def view(self, run_id):
        run = self.store.get(run_id)
        if not run.get("test_change_proposal"):
            return {"proposal": None, "error": None}
        proposal = run["test_change_proposal"]
        error, patch = None, ""
        try:
            _, result = self.load(run)
            patch = result.patch
        except (PipelineError, OSError, ValueError, KeyError):
            error = (
                "제안 내용이나 실행 기준이 변경되어 승인할 수 없습니다. 반려 후 다시 작성하세요."
            )
        return {
            "proposal": {
                k: proposal[k]
                for k in [
                    "id",
                    "proposal_hash",
                    "input_sha",
                    "patch_hash",
                    "role",
                    "revision",
                    "iteration",
                    "changes",
                    "created_at",
                ]
            }
            | {"patch": patch, "approved": bool(proposal.get("approval"))},
            "error": error,
        }

    def verify(self, run, proposal_hash, input_sha, *, approve=True):
        if run["state"] not in {"needs_input", "paused", "failed"} or (
            run.get("control") and not (run["state"] == "paused" and run["control"] == "pause")
        ):
            raise PipelineError("test change decision requires an idle needs_input run")
        proposal = run.get("test_change_proposal")
        if not proposal:
            raise PipelineError("no protected test change proposal")
        if proposal["proposal_hash"] != proposal_hash or proposal["input_sha"] != input_sha:
            raise PipelineError("test change proposal changed; review the current diff")
        if run.get("worktree_removed") or run.get("cleanup_requested"):
            raise PipelineError("test change worktree is unavailable")
        # Discarding an unapproved proposal never mutates Git and must remain
        # possible when its artifact or working context has become stale.
        if not approve:
            if proposal.get("approval"):
                git = Git(
                    Path(run["worktree"]), Config.model_validate(run["config"]).tests.output_paths
                )
                if run["current_sha"] != input_sha or git.sha() != input_sha or git.dirty():
                    raise PipelineError(
                        "approved patch may have been applied; inspect preserved worktree"
                    )
            return proposal
        proposal, result = self.load(run)
        git = Git(Path(run["worktree"]), Config.model_validate(run["config"]).tests.output_paths)
        if git.sha() != input_sha or git.dirty():
            raise PipelineError("test change approval requires unchanged HEAD and a clean worktree")
        if proposal.get("approval"):
            raise PipelineError("test change proposal already approved")
        checkpoints(self.store, run)
        try:
            validate_patch(
                Path(run["worktree"]),
                result.patch,
                proposal["role"],
                Config.model_validate(run["config"]).tests.write_paths,
            )
        except TestChangesRequired as error:
            if set(error.paths) != {c["file"] for c in proposal["changes"]}:
                raise PipelineError("protected test paths changed") from error
        else:
            raise PipelineError("proposal no longer changes protected tests")
        return proposal

    def decision_changes(self, run, proposal, approve, reason=""):
        if approve:
            approval = {
                "proposal_id": proposal["id"],
                "proposal_hash": proposal["proposal_hash"],
                "patch_hash": proposal["patch_hash"],
                "input_sha": proposal["input_sha"],
                "role": proposal["role"],
                "iteration": proposal["iteration"],
                "revision": proposal["revision"],
                "changes": proposal["changes"],
                "approved_version": run["version"],
                "approved_at": now(),
            }
            return {
                "test_change_proposal": proposal | {"approval": approval},
                "test_change_approvals": [*run.get("test_change_approvals", []), approval],
            }
        reason = sanitize(reason)
        if not reason.strip():
            raise PipelineError("rejecting test changes requires instructions")
        return {
            "test_change_proposal": None,
            "test_change_approvals": [
                approval
                for approval in run.get("test_change_approvals", [])
                if approval["proposal_id"] != proposal["id"]
            ],
            "directives": [*run.get("directives", []), "테스트 변경 반려: " + reason],
        }

    def approved_result(self, run):
        proposal, result = self.load(run)
        approval = proposal.get("approval")
        if not approval or approval not in run.get("test_change_approvals", []):
            raise PipelineError("explicit test change approval required before applying patch")
        for key in ["proposal_hash", "patch_hash", "input_sha", "role", "iteration", "revision"]:
            if approval[key] != proposal[key]:
                raise PipelineError("test change approval binding mismatch")
        if approval["proposal_id"] != proposal["id"]:
            raise PipelineError("test change approval identity mismatch")
        return result, approval
