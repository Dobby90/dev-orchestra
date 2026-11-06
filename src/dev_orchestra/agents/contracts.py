"""Workspace wire responses and host-collected artifact validation."""

from dev_orchestra.domain.models import PipelineError


class ResponseError(PipelineError):
    """A model response can be regenerated without changing the execution input."""


# Writable-role wire responses use WorkspaceResult. These fields describe artifacts
# whose patch and test changes are collected by the host from verified workspace files.
ARTIFACT_FIELDS = {
    "planner": {"summary", "questions", "plan", "criteria"},
    "developer": {"summary", "questions", "patch", "test_changes"},
    "fixer": {"summary", "questions", "patch", "test_changes"},
    "tester": {"summary", "questions", "patch", "test_changes", "command_ids", "coverage"},
    "reviewer": {"summary", "questions", "issues"},
}


def response_fields(request):
    from dev_orchestra.domain.execution import writes_workspace

    if writes_workspace(request):
        fields = {"summary", "questions", "no_change_reason", "commit_message"}
        if request.role == "tester":
            fields |= {"command_ids", "coverage", "test_evidence_id"}
        return fields
    fields = ARTIFACT_FIELDS[request.role].copy()
    if request.role == "reviewer" and request.context.get("review_contract") == 2:
        fields |= {"schema_version", "assessments"}
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
    ):
        if key not in allowed and getattr(result, key):
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
