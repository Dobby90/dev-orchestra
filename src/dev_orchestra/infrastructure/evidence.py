"""Validate durable evidence before reuse and before approving a commit."""

import json
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import digest


def checkpoints(store, run):
    latest = {}
    try:
        for step in store.steps(run["id"]):
            if step["status"] != "completed" and not (
                step["status"] == "failed" and step["data"].get("artifact")
            ):
                continue
            data = step["data"]
            artifact = Path(data["artifact"])
            if digest(artifact.read_bytes()) != data["hash"]:
                raise PipelineError("checkpoint artifact modified (checksum mismatch)")
            payload = json.loads(artifact.read_text())
            if payload["sha"] != step["output_sha"]:
                raise PipelineError("checkpoint SHA mismatch")
            if payload.get("test_evidence_reference"):
                from dev_orchestra.application.test_selection import verify_tool_evidence

                verify_tool_evidence(run, step, payload)
            if "workspace_commits" in payload:
                verify_workspace_commits(run, step, payload)
            approval = payload.get("test_change_approval")
            if approval:
                if (
                    approval not in run.get("test_change_approvals", [])
                    or approval["patch_hash"] != digest(payload["patch"].encode())
                    or approval["input_sha"] != step["input_sha"]
                    or approval["role"] != step["role"]
                    or approval["iteration"] != step["iteration"]
                    or approval["revision"] != payload.get("revision", 0)
                ):
                    raise PipelineError("protected test approval evidence mismatch")
            record = payload.get("test_change_record")
            if record:
                from dev_orchestra.application.test_changes import checksum

                if (
                    record not in run.get("test_change_records", [])
                    or checksum({k: v for k, v in record.items() if k != "record_hash"})
                    != record["record_hash"]
                    or record["run_id"] != run["id"]
                    or record["patch_hash"] != digest(payload["patch"].encode())
                    or record["changes"] != payload["test_changes"]
                    or record["input_sha"] != step["input_sha"]
                    or record["role"] != step["role"]
                    or record["iteration"] != step["iteration"]
                    or record["revision"] != payload.get("revision", 0)
                ):
                    raise PipelineError("protected test change evidence mismatch")
            if step["status"] == "completed":
                latest[step["role"]] = payload
        if run.get("test_change_application"):
            from dev_orchestra.application.test_changes import TestChangeService

            TestChangeService(store).applied_result(run)
        return latest
    except (OSError, ValueError, KeyError) as error:
        raise PipelineError("missing or invalid checkpoint evidence") from error


def verify_workspace_commits(run, step, payload):
    """Checkpoint commits are stage evidence, never test/review success evidence."""
    from dev_orchestra.application.test_changes import checksum
    from dev_orchestra.domain.execution import WORKSPACE_V2, execution_contract
    from dev_orchestra.infrastructure.workspace import workspace_policy_hash

    if execution_contract(run) != WORKSPACE_V2 or step["role"] not in {
        "developer",
        "fixer",
        "tester",
    }:
        raise PipelineError("unexpected workspace commit evidence")
    parent, seen = step["input_sha"], set()
    for commit in payload["workspace_commits"]:
        args = {
            key: commit[key]
            for key in ["request_id", "expected_sha", "files", "message", "summary"]
        }
        if (
            commit["parent"] != parent
            or commit["expected_sha"] != parent
            or commit["request_id"] in seen
            or not commit["files"]
            or commit["request_hash"] != checksum(args)
            or any(
                commit[key] != value
                for key, value in {
                    "run_id": run["id"],
                    "role": step["role"],
                    "iteration": step["iteration"],
                    "revision": payload.get("revision", 0),
                    "step_id": step["id"],
                    "input_sha": step["input_sha"],
                    "policy_hash": workspace_policy_hash(run)
                    if commit.get("policy_hash_version") == 2
                    else checksum(run["config"]),
                }.items()
            )
        ):
            raise PipelineError("workspace commit chain or ownership mismatch")
        seen.add(commit["request_id"])
        parent = commit["sha"]
    if parent != step["output_sha"]:
        raise PipelineError("workspace stage output does not match checkpoint chain")


def verified_candidate(store, run, sha):
    latest = checkpoints(store, run)
    tests, review = latest.get("tester"), latest.get("reviewer")
    if not tests or not review or any(p["sha"] != sha for p in [tests, review]):
        raise PipelineError("candidate SHA has not passed tests and independent review")
    if run.get("test_result") != tests or run.get("review_result") != review:
        raise PipelineError("cached evidence differs from checkpoint artifacts")
    from dev_orchestra.application.test_selection import verify_selection

    verify_selection(run, tests)
    if not tests["execution"]["passed"]:
        raise PipelineError("candidate tests failed")
    blocking = run["config"]["review"]["blocking_severities"]
    if any(i["severity"] in blocking for i in review["issues"]):
        raise PipelineError("blocking review findings remain")

    if run.get("review_contract") == 2:
        revision = run.get("revision", 0)
        if any(p.get("revision", 0) != revision for p in [tests, review]):
            raise PipelineError("candidate revision has stale evidence")
        if review.get("schema_version") != 2 or "issue_states" not in review:
            raise PipelineError("missing explicit review assessments")
        if any(
            i["severity"] in blocking and i["status"] == "unresolved"
            for i in review["issue_states"]
        ):
            raise PipelineError("unresolved blocking review findings remain")

        from dev_orchestra.application.reviews import Reviews

        if any(i["severity"] in blocking for i in Reviews(store).open(run["id"])):
            raise PipelineError("open blocking issue ledger differs from review evidence")
