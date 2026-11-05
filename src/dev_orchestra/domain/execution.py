"""Persisted execution contracts. Missing identifiers always retain legacy behavior."""

from dev_orchestra.domain.models import PipelineError

PATCH_V1 = "patch-v1"
WORKSPACE_V2 = "workspace-v2"


def execution_contract(run):
    value = run.get("execution_contract", PATCH_V1)
    if value not in {PATCH_V1, WORKSPACE_V2}:
        raise PipelineError("unsupported execution contract")
    return value


def writes_workspace(request):
    return execution_contract(request.context) == WORKSPACE_V2 and request.role in {
        "developer",
        "fixer",
        "tester",
    }
