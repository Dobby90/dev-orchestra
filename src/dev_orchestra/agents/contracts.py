"""Workspace wire responses and host-collected artifact validation."""

from collections import Counter

from dev_orchestra.domain.models import (
    PipelineError,
    WorkspaceFixResult,
    WorkspaceResult,
    WorkspaceTestResult,
)


class ResponseError(PipelineError):
    """A model response can be regenerated without changing the execution input."""


# Writable-role wire responses use WorkspaceResult. These fields describe artifacts
# whose patch and test changes are collected by the host from verified workspace files.
ARTIFACT_FIELDS = {
    "planner": {"summary", "questions", "plan", "criteria"},
    "developer": {"summary", "questions", "patch", "test_changes"},
    "fixer": {"summary", "questions", "patch", "test_changes", "issue_outcomes"},
    "tester": {"summary", "questions", "patch", "test_changes", "command_ids", "coverage"},
    "reviewer": {"summary", "questions", "issues"},
}


def workspace_response_model(role):
    if role == "fixer":
        return WorkspaceFixResult
    if role == "tester":
        return WorkspaceTestResult
    return WorkspaceResult


def response_fields(request):
    from dev_orchestra.domain.execution import writes_workspace

    if writes_workspace(request):
        return set(workspace_response_model(request.role).model_fields)
    fields = ARTIFACT_FIELDS[request.role].copy()
    if request.role == "reviewer" and request.context.get("review_contract") == 2:
        fields |= {"schema_version", "assessments", "issue_distinctions"}
    return fields


def validate_role(role, result):
    allowed = ARTIFACT_FIELDS[role] | {"schema_version", "assessments"}
    if role == "tester":
        allowed.add("test_plan")
    for key in (
        "plan",
        "criteria",
        "patch",
        "command_ids",
        "coverage",
        "issues",
        "test_changes",
        "test_plan",
        "issue_outcomes",
    ):
        if key not in allowed and getattr(result, key, None):
            raise ResponseError(f"{role} must not return {key}; return only role-specific results")
    if not result.summary.strip():
        raise ResponseError("response requires a nonempty summary")
    if (
        role == "planner"
        and not result.questions
        and (
            not result.plan.strip()
            or not result.criteria
            or len({c.id for c in result.criteria}) != len(result.criteria)
        )
    ):
        raise ResponseError("planner requires a plan and unique nonempty acceptance criteria")


def validate_fixer_outcomes(outcomes, issue_ids):
    expected = set(issue_ids)
    returned = Counter(item.issue_id for item in outcomes)
    problems = {
        "missing": sorted(expected - returned.keys()),
        "duplicate": sorted(issue_id for issue_id, count in returned.items() if count > 1),
        "unknown": sorted(returned.keys() - expected),
    }
    if any(problems.values()):
        details = ", ".join(f"{kind} IDs: {ids!r}" for kind, ids in problems.items() if ids)
        raise ResponseError(
            "Fixer issue_outcomes must cover response_contract.issue_ids exactly once. " + details
        )
