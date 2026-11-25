"""Transport-independent workflow contracts."""

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_DESIGN_IMAGES = 8
MAX_DESIGN_IMAGE_BYTES = 5 * 2**20


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


class TestTarget(Model):
    command_id: str
    files: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)


class AgentResult(Model):
    commit_message: str = ""
    summary: str
    questions: list[str] = Field(default_factory=list)
    plan: str = ""
    criteria: list[Criterion] = Field(default_factory=list)
    patch: str = ""
    command_ids: list[str] = Field(default_factory=list)
    coverage: list[Coverage] = Field(default_factory=list)
    test_plan: list[TestTarget] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    test_changes: list[TestChange] = Field(default_factory=list)


class AgentRequest(Model):
    run_id: str
    role: Literal["planner", "developer", "tester", "reviewer", "fixer"]
    worktree: str
    iteration: int
    input_sha: str
    context: dict
    design_image_paths: tuple[Path, ...] = Field(default=(), exclude=True)


class DesignImage(Model):
    name: str = Field(min_length=5, max_length=255)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0, le=MAX_DESIGN_IMAGE_BYTES)

    @field_validator("name")
    @classmethod
    def png_filename(cls, name):
        if (
            not name.isprintable()
            or "/" in name
            or "\\" in name
            or not name.lower().endswith(".png")
        ):
            raise ValueError("design reference requires a PNG filename")
        return name


class WorkspaceResult(Model):
    commit_message: str = ""
    summary: str = Field(min_length=1)
    questions: list[str] = Field(default_factory=list)
    no_change_reason: str = ""


class FixIssueOutcome(Model):
    issue_id: str = Field(min_length=1)
    status: Literal["implemented", "partial", "blocked"]
    evidence: str = Field(min_length=1)

    @field_validator("evidence")
    @classmethod
    def nonblank_evidence(cls, evidence):
        if not evidence.strip():
            raise ValueError("fixer issue outcome requires evidence")
        return evidence


class WorkspaceFixResult(WorkspaceResult):
    issue_outcomes: list[FixIssueOutcome] = Field(default_factory=list)


class FixerResult(AgentResult):
    issue_outcomes: list[FixIssueOutcome]


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


class ReviewIssueDistinction(Model):
    finding_id: str
    existing_issue_id: str
    reason: str = Field(min_length=1)


class ReviewResultV2(AgentResult):
    schema_version: Literal[2] = 2
    assessments: list[ReviewAssessment] = Field(default_factory=list)
    issue_distinctions: list[ReviewIssueDistinction] = Field(default_factory=list)
