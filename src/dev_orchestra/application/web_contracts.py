"""Public, bounded web contracts; independent of FastAPI."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from dev_orchestra.config import Tests


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Policy(Input):
    tests: Tests
    max_fix_attempts: int = Field(default=3, ge=0, le=20, strict=True)
    blocking_severities: list[Literal["critical", "high", "medium", "low"]] = Field(
        default_factory=lambda: ["critical", "high"]
    )


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


class Reason(Input):
    reason: str = Field(default="", max_length=4000)


class ProjectView(BaseModel):
    schema_version: Literal[1] = 1
    id: str
    name: str
    repository_path: str
    default_base_branch: str
    policy: Policy
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


class ResumeInput(Input):
    answers_text: str | None = Field(default=None, max_length=65536)
    new_max_fix_attempts: int | None = Field(default=None, gt=0, le=20, strict=True)
