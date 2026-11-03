"""Strict YAML configuration, resolved once and snapshotted per run."""

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import Field, StringConstraints, model_validator

from dev_orchestra.domain.models import Model


class Project(Model):
    default_base_branch: str = "main"
    branch_prefix: str = "feature/ai"


Role = Literal["planner", "developer", "tester", "reviewer", "fixer"]
ROLES = ("planner", "developer", "tester", "reviewer", "fixer")
ModelName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")]
ReasoningEffort = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]


class AgentOptions(Model):
    model: ModelName | None = None
    reasoning_effort: ReasoningEffort | None = None


class AgentSettings(AgentOptions):
    roles: dict[Role, AgentOptions] = Field(default_factory=dict)

    def for_role(self, role: str) -> AgentOptions:
        override = self.roles.get(role, AgentOptions())
        return AgentOptions(
            model=override.model or self.model,
            reasoning_effort=override.reasoning_effort or self.reasoning_effort,
        )


class Codex(AgentSettings):
    command: str = "codex"
    timeout_seconds: int = Field(default=1800, gt=0)
    role_timeouts: dict[str, int] = Field(default_factory=dict)


class Workflow(Model):
    max_fix_attempts: int = Field(default=3, ge=0)
    auto_commit: Literal[True] = True
    require_human_approval: Literal[True] = True


class Review(Model):
    blocking_severities: list[Literal["critical", "high", "medium", "low"]] = Field(
        default_factory=lambda: ["critical", "high"]
    )
    include_test_files: Literal[True] = True


class Git(Model):
    auto_push: Literal[False] = False
    auto_merge: Literal[False] = False
    keep_worktree_on_failure: Literal[True] = True


class Storage(Model):
    database: Path = Path("~/.devorchestra/pipeline.db")
    runs_directory: Path = Path("~/.devorchestra/runs")
    worktrees_directory: Path = Path("~/.devorchestra/worktrees")


class TestCommand(Model):
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=600, gt=0)
    junit_report: str


class Tests(Model):
    commands: dict[str, TestCommand] = Field(default_factory=dict)
    write_paths: list[str] = Field(default_factory=lambda: ["tests"])
    output_paths: list[str] = Field(default_factory=lambda: [".pytest_cache", "build", "target"])


class Config(Model):
    project: Project = Field(default_factory=Project)
    codex: Codex = Field(default_factory=Codex)
    workflow: Workflow = Field(default_factory=Workflow)
    review: Review = Field(default_factory=Review)
    git: Git = Field(default_factory=Git)
    storage: Storage = Field(default_factory=Storage)
    tests: Tests = Field(default_factory=Tests)

    @model_validator(mode="after")
    def validate_policy(self):
        from dev_orchestra.infrastructure.files import relative_path

        for path in self.tests.write_paths + self.tests.output_paths:
            relative_path(path)
        import re

        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", key) for key in self.tests.commands):
            raise ValueError("test command IDs must be safe identifiers")
        for command in self.tests.commands.values():
            relative_path(command.junit_report)
            if any(x in command.argv for x in ["--passWithNoTests", "--collect-only"]):
                raise ValueError("test command must execute tests")
        if any(v <= 0 for v in self.codex.role_timeouts.values()):
            raise ValueError("role timeout must be positive")
        return self


def load_config(path: Path | None = None) -> Config:
    from dev_orchestra.domain.models import PipelineError

    try:
        config = Config.model_validate(yaml.safe_load(path.read_text()) or {}) if path else Config()
    except yaml.YAMLError as error:
        raise PipelineError("설정 YAML 구문이 올바르지 않습니다") from error
    root = path.resolve().parent if path else Path.cwd()
    for name in Config.model_fields["storage"].annotation.model_fields:
        value = getattr(config.storage, name).expanduser()
        setattr(config.storage, name, (root / value).resolve())
    return config
