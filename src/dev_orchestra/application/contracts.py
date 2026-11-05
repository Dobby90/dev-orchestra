"""Application contracts shared by adapters; no HTTP or CLI dependencies."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from dev_orchestra.domain.models import State


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CreateRun(Contract):
    repo: Path
    spec: Path
    base: str | None = None
    max_fixes: int | None = Field(default=None, ge=0, strict=True)
    test_scope: Literal["full", "affected"] = "full"


class ApprovalCommand(Contract):
    run_id: str
    expected_version: int = Field(ge=0, strict=True)
    expected_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class RunSummary(Contract):
    schema_version: Literal[1] = 1
    id: str
    state: State
    version: int
    created_at: str | None = None
    completed_at: str | None = None
    branch: str | None = None
    base_sha: str | None = None
    current_sha: str | None = None
    final_sha: str | None = None
    iteration: int = 0
    current_role: str | None = None
    control: Literal["cancel", "pause"] | None = None


class RepositoryInfo(Contract):
    repository_path: Path
    git_common_directory: Path
    base_branch: str
    base_sha: str
