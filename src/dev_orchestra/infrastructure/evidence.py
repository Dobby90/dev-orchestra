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
            if step["status"] == "completed":
                latest[step["role"]] = payload
        return latest
    except (OSError, ValueError, KeyError) as error:
        raise PipelineError("missing or invalid checkpoint evidence") from error


def verified_candidate(store, run, sha):
    latest = checkpoints(store, run)
    tests, review = latest.get("tester"), latest.get("reviewer")
    if not tests or not review or any(p["sha"] != sha for p in [tests, review]):
        raise PipelineError("candidate SHA has not passed tests and independent review")
    if run.get("test_result") != tests or run.get("review_result") != review:
        raise PipelineError("cached evidence differs from checkpoint artifacts")
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
