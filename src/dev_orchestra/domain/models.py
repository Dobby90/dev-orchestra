"""Transport-independent workflow contracts."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class State(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    PLANNING = "planning"
    WAITING_PLAN_APPROVAL = "waiting_plan_approval"
    DEVELOPING = "developing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    FIXING = "fixing"
    REPORTING = "reporting"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    NEEDS_INPUT = "needs_input"
    PAUSED = "paused"
    CANCELLED = "cancelled"


class Criterion(Model):
    id: str = Field(min_length=1)
    description: str = Field(min_length=1)


class Issue(Model):
    id: str
    severity: Literal["critical", "high", "medium", "low"]
    file: str
    line: int = Field(ge=1)
    description: str
    evidence: str
    suggestion: str


class Coverage(Model):
    criterion_id: str
    command_ids: list[str]


class TestChange(Model):
    file: str
    criterion_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=1)


class AgentResult(Model):
    summary: str
    questions: list[str] = Field(default_factory=list)
    plan: str = ""
    criteria: list[Criterion] = Field(default_factory=list)
    patch: str = ""
    command_ids: list[str] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    test_changes: list[TestChange] = Field(default_factory=list)


class AgentRequest(Model):
    run_id: str
    role: Literal["planner", "developer", "tester", "reviewer", "fixer"]
    worktree: str
    iteration: int
    input_sha: str
    context: dict


class WorkspaceResult(Model):
    summary: str = Field(min_length=1)
    questions: list[str] = Field(default_factory=list)
    no_change_reason: str = ""


class WorkspaceTestResult(WorkspaceResult):
    command_ids: list[str] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    test_evidence_id: str = ""


class PipelineError(RuntimeError):
    """Safe, user-facing workflow failure."""


class StopRequested(PipelineError):
    pass


class ReviewAssessment(Model):
    issue_id: str
    status: Literal["resolved", "unresolved"]
    candidate_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    evidence: str = Field(min_length=1)


class ReviewResultV2(AgentResult):
    schema_version: Literal[2] = 2
    assessments: list[ReviewAssessment] = Field(default_factory=list)
