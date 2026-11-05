"""Role response boundaries; durable artifacts retain the legacy common shape."""

from dev_orchestra.domain.models import PipelineError


class ResponseError(PipelineError):
    """A model response can be regenerated without changing the execution input."""


FIELDS = {
    "planner": {"summary", "questions", "plan", "criteria"},
    "developer": {"summary", "questions", "patch", "test_changes", "no_change_reason"},
    "fixer": {"summary", "questions", "patch", "test_changes", "no_change_reason"},
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
    fields = FIELDS[request.role].copy()
    if request.role in {"developer", "fixer", "tester"}:
        fields.add("commit_message")
    if request.role == "tester" and request.context.get("automatic_test_selection"):
        fields.add("test_plan")
    if request.role == "reviewer" and request.context.get("review_contract") == 2:
        fields |= {"schema_version", "assessments"}
    return fields


def validate_role(role, result):
    allowed = FIELDS[role] | {"schema_version", "assessments"}
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
    if result.patch and result.commit_message.strip():
        from dev_orchestra.infrastructure.commit_messages import validate_commit_message

        try:
            validate_commit_message(result.commit_message)
        except PipelineError as error:
            raise ResponseError(str(error)) from error
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
