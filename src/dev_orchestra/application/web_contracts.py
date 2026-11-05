"""Public, bounded web contracts; independent of FastAPI."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dev_orchestra.application.contracts import RunSummary
from dev_orchestra.config import AgentOptions, AgentSettings, TestCommand, Tests
from dev_orchestra.domain.models import State
from dev_orchestra.infrastructure.agent_usage import AgentTelemetry, TokenUsage


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentSettingsInput(Input):
    settings: AgentSettings
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentSettingsView(BaseModel):
    settings: AgentSettings
    version: str


class CodexModelView(BaseModel):
    model: str
    reasoning_efforts: list[str]


class CodexModelPage(BaseModel):
    items: list[CodexModelView]


class AgentAttemptView(AgentTelemetry):
    step_id: int
    role: str
    revision: int
    iteration: int
    attempt: int
    status: str


class AgentUsageView(BaseModel):
    agents: dict[str, AgentOptions]
    attempts: list[AgentAttemptView]
    usage: TokenUsage | None
    usage_status: Literal["reported", "partial", "unknown"]
    measured_attempts: int
    total_attempts: int


class Policy(Input):
    tests: Tests
    max_fix_attempts: int = Field(default=3, ge=0, le=20, strict=True)
    blocking_severities: list[Literal["critical", "high", "medium", "low"]] = Field(
        default_factory=lambda: ["critical", "high"]
    )


class ValidationDetectInput(Input):
    repository_path: str = Field(min_length=1, max_length=4096)
    base_branch: str = "main"


class ValidationCandidate(BaseModel):
    id: str
    label: str
    evidence: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    command: TestCommand
    write_paths: list[str]
    output_paths: list[str]


class ValidationDetection(BaseModel):
    base_sha: str
    candidates: list[ValidationCandidate]
    warnings: list[str]


class ProjectCreate(Input):
    name: str = Field(min_length=1, max_length=100)
    repository_path: str = Field(min_length=1, max_length=4096)
    default_base_branch: str = "main"
    policy: Policy


class ProjectPatch(Input):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    default_base_branch: str | None = None
    policy: Policy | None = None


class TextSpec(Input):
    kind: Literal["text"]
    text: str = Field(min_length=1, max_length=1048576)


class StoredSpec(Input):
    kind: Literal["stored"]
    spec_id: str = Field(pattern=r"^spec-[0-9a-f]{32}$")


class Proposal(Input):
    test_scope: Literal["full", "affected"] = "full"
    project_id: str = Field(pattern=r"^prj-[0-9a-f]{32}$")
    spec: Annotated[TextSpec | StoredSpec, Field(discriminator="kind")]
    base_branch: str | None = None
    feature_branch: str | None = None
    max_fix_attempts: int | None = Field(default=None, ge=0, le=20, strict=True)
    additional_instructions: str = Field(default="", max_length=65536)
    run_tests: Literal[True] = True
    run_review: Literal[True] = True


class RunCreate(Proposal):
    preview_token: str = Field(max_length=4096)


class SessionCreate(Input):
    bootstrap_code: str = Field(min_length=32, max_length=128)


class ApprovalInput(Input):
    expected_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class TestChangeDecisionInput(Input):
    proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    reason: str = Field(default="", max_length=4000)


class TestChangeExplanation(BaseModel):
    file: str
    criterion_ids: list[str]
    reason: str


class TestChangeProposalView(BaseModel):
    id: str
    proposal_hash: str
    input_sha: str
    patch_hash: str
    role: str
    revision: int
    iteration: int
    changes: list[TestChangeExplanation]
    created_at: str
    patch: str
    approved: bool


class TestChangeRecordView(BaseModel):
    id: str
    role: str
    iteration: int
    revision: int
    input_sha: str
    output_sha: str
    changes: list[TestChangeExplanation]
    patch: str


class TestChangeView(BaseModel):
    proposal: TestChangeProposalView | None = None
    error: str | None = None
    history: list[TestChangeRecordView] = Field(default_factory=list)


class Reason(Input):
    reason: str = Field(default="", max_length=4000)


class ProjectView(BaseModel):
    schema_version: Literal[1] = 1
    id: str
    name: str
    repository_path: str
    default_base_branch: str
    policy: Policy
    branch_prefix: str = "feature/ai"
    version: int
    created_at: str
    updated_at: str
    archived_at: str | None


class ProjectPage(BaseModel):
    items: list[ProjectView]
    has_more: bool
    next_cursor: str | None


class SubmissionView(BaseModel):
    schema_version: Literal[1] = 1
    run_id: str
    job_id: str
    operation_id: str
    status: Literal["queued"]
    queued_reason: Literal["dispatcher_offline", "waiting_capacity"]
    run_version: int


class SessionView(BaseModel):
    authenticated: Literal[True] = True
    expires_at: datetime
    csrf_token: str


class PlanDecisionInput(Input):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(default="", max_length=65536)


class ResumeInput(Input):
    answers_text: str | None = Field(default=None, max_length=65536)
    new_max_fix_attempts: int | None = Field(default=None, gt=0, le=20, strict=True)


# Existing read models are explicit so the browser consumes generated OpenAPI types.
class RunPage(BaseModel):
    items: list[RunSummary]
    has_more: bool
    next_cursor: str | None


class ExecutionView(BaseModel):
    job_state: str | None = None
    worker_liveness: Literal["alive", "stopped", "lost", "unresponsive", "unknown"] = "unknown"
    heartbeat_at: str | None = None
    cleanup: Literal["confirmed", "pending", "manual_required", "unknown"] = "unknown"
    worktree_removed: bool = False


class ValidationView(BaseModel):
    status: Literal["pending", "valid", "invalid", "unknown"] = "unknown"
    revision: int = 0
    candidate_sha: str | None = None
    test_sha: str | None = None
    review_sha: str | None = None
    checked_at: str | None = None
    reason: str | None = None


class RunDetail(RunSummary):
    test_scope: Literal["full", "affected"] = "full"
    plan: str | None = None
    criteria: list[dict[str, str]] = Field(default_factory=list)
    plan_hash: str | None = None
    deletion_pending: bool = False
    revision: int = 0
    execution: ExecutionView = Field(default_factory=ExecutionView)
    validation: ValidationView = Field(default_factory=ValidationView)
    allowed_actions: list[str] = Field(default_factory=list)
    event_cursor: int
    project_id: str | None
    base_branch: str | None
    created_at: str | None
    max_fix_attempts: int | None
    questions: list[str]
    error: str | None
    test_change_pending: bool = False
    test_change_count: int = 0


class DashboardView(BaseModel):
    counts_by_state: dict[State, int]
    project_count: int


class BranchPage(BaseModel):
    items: list[str]


class SpecView(BaseModel):
    id: str
    sha256: str
    bytes: int


class SpecContent(BaseModel):
    text: str
    additional_instructions: str


class PreviewView(BaseModel):
    test_scope: Literal["full", "affected"] = "full"
    test_scope_notice: str = ""
    run_id: str
    branch: str
    base_sha: str
    project_version: int
    agent_settings_hash: str = ""
    proposal_hash: str
    expires: int
    preview_token: str
    spec_sha256: str
    validation_warnings: list[str] = Field(default_factory=list)


class StepView(BaseModel):
    id: int
    run_id: str
    role: str
    iteration: int
    attempt: int
    input_sha: str
    output_sha: str | None
    status: str
    revision: int = 0
    started_at: str | None = None
    finished_at: str | None = None


class StepPage(BaseModel):
    items: list[StepView]


class EventEnvelope(BaseModel):
    schema_version: Literal[1] = 1
    id: int
    run_id: str
    kind: str
    occurred_at: str
    data: dict[str, Any]


class EventPage(BaseModel):
    items: list[EventEnvelope]
    next_cursor: int
    has_more: bool


class LogWindow(BaseModel):
    items: list[EventEnvelope]
    latest_cursor: int
    before_cursor: int
    has_more: bool


class LogPage(EventPage):
    schema_version: Literal[2] = 2


class ReportView(BaseModel):
    markdown: str
    artifact_id: str | None = None
    generated_version: int | None = None
    current_version: int | None = None
    stale: bool = True


class ControlResult(BaseModel):
    state: State


class ResumeView(BaseModel):
    run_id: str
    job_id: str
    status: Literal["queued"]


class ChangeInput(Input):
    expected_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    instructions: str = Field(min_length=1, max_length=65536)
    new_max_fix_attempts: int | None = Field(default=None, gt=0, le=20, strict=True)


class DeferInput(Input):
    reason: str = Field(min_length=1, max_length=4000)


class CleanupInput(Input):
    confirm: Literal[True]


class DispositionInput(DeferInput):
    status: Literal["unresolved", "dismissed"]


class OperationView(BaseModel):
    id: str
    run_id: str | None
    kind: str
    status: Literal["accepted", "applied", "rejected", "needs_input"]
    state: State
    result: dict[str, Any]
    created_at: str
    applied_at: str | None
    error: str | None


class StepStamp(BaseModel):
    id: int
    revision: int | None
    iteration: int
    attempt: int
    status: str
    output_sha: str | None
    started_at: str | None
    finished_at: str | None


class FailureView(BaseModel):
    name: str
    classname: str | None = None
    file: str | None = None
    line: str | int | None = None
    message: str = ""
    details: str = ""


class CommandView(BaseModel):
    command_id: str | None = None
    kind: Literal["test", "check"] | None = None
    working_directory: str | None = None
    argv: list[str] | None = None
    status: str = "unknown"
    exit_code: int | None = None
    duration_seconds: float | None = None
    total: int | None = None
    failed: int | None = None
    skipped: int | None = None
    passed: bool | None = None
    failures: list[str] = Field(default_factory=list)
    failure_details: list[FailureView] = Field(default_factory=list)
    junit_artifact_id: str | None = None
    error: str | None = None


class TestSelectionView(BaseModel):
    command_reasons: dict[str, str] = Field(default_factory=dict)
    input_error: str | None = None
    selector_version: int
    requested_scope: Literal["full", "affected"]
    actual_scope: Literal["full", "affected"]
    expanded: bool
    reasons: list[str]
    included: list[str]
    excluded: list[str]
    changed_files: list[str]
    base_sha: str
    candidate_sha: str
    revision: int
    policy_hash: str
    criteria_hash: str
    commands_hash: str
    selection_hash: str


class TestResultView(BaseModel):
    selection: TestSelectionView | None = None
    status: Literal[
        "not_run", "running", "passed", "failed", "unavailable", "parse_error", "timeout", "unknown"
    ]
    step_id: int | None
    sha: str | None
    revision: int | None
    duration_seconds: float | None
    total: int | None
    passed: int | None
    failed: int | None
    skipped: int | None
    commands: list[CommandView]
    failures: list[FailureView]
    history: list[StepStamp]
    legacy_incomplete: bool


class ReviewIssueView(BaseModel):
    issue_id: str
    id: str
    severity: Literal["critical", "high", "medium", "low"]
    file: str
    line: int
    description: str
    evidence: str
    suggestion: str
    status: Literal["unresolved", "resolved", "dismissed"]
    source_sha: str
    resolution_evidence: str | None
    status_note: str | None = None
    assessed_sha: str | None = None
    assessed_step_id: int | None = None


class ReviewResultView(BaseModel):
    status: Literal["not_run", "running", "available", "unavailable"]
    step_id: int | None
    sha: str | None
    revision: int | None
    summary: str
    issues: list[ReviewIssueView]
    history: list[StepStamp]
    legacy_incomplete: bool
    blocking_severities: list[str]


class ChangedFile(BaseModel):
    file_id: str
    path: str
    status: str
    binary: bool
    truncated: bool
    additions: int | None
    deletions: int | None
    artifact_id: str


class SnapshotStamp(BaseModel):
    revision: int
    candidate_sha: str


class ChangesView(BaseModel):
    available: bool
    revision: int
    base_sha: str | None
    merge_base_sha: str | None
    candidate_sha: str | None
    files: list[ChangedFile]
    history: list[SnapshotStamp]


class DiffView(ChangedFile):
    base_sha: str
    candidate_sha: str
    text: str


class NotificationView(BaseModel):
    id: int
    run_id: str
    occurred_at: str
    state: Literal["needs_input", "paused", "waiting_approval", "waiting_plan_approval", "failed"]
    read: bool


class NotificationPage(BaseModel):
    items: list[NotificationView]
    unread_count: int
    cursor: int
    next_cursor: int | None


class NotificationRead(Input):
    ids: list[Annotated[int, Field(ge=1, strict=True)]] = Field(
        default_factory=list, max_length=100
    )
    through: int | None = Field(default=None, ge=0, strict=True)
