"""Workspace-only execution; historical contracts remain readable, never executable."""

from dev_orchestra.domain.models import PipelineError

WORKSPACE_V2 = "workspace-v2"


def execution_contract(run):
    if run.get("execution_contract") != WORKSPACE_V2:
        raise PipelineError(
            "unsupported execution contract; historical runs are read-only; create a new workspace-v2 run"
        )
    return WORKSPACE_V2


def writes_workspace(request):
    return request.role in {"developer", "fixer", "tester"}
