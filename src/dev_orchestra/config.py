"""Strict YAML configuration, resolved once and snapshotted per run."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, model_validator

from dev_orchestra.domain.models import Model


class Project(Model):
    default_base_branch: str = "main"
    branch_prefix: str = "feature/ai"


class Codex(Model):
    command: str = "codex"
    model: str | None = None
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
    kind: Literal["test", "check"] = "test"
    argv: list[str] = Field(min_length=1)
    timeout_seconds: int = Field(default=600, gt=0)
    working_directory: str = "."
    junit_report: str | None = None
    env: dict[
        Literal["PLAYWRIGHT_JUNIT_OUTPUT_FILE", "PLAYWRIGHT_BROWSERS_PATH", "JAVA_HOME"], str
    ] = Field(default_factory=dict)
    network: Literal["none", "loopback"] = "none"

    @model_validator(mode="after")
    def validate_command(self):
        from dev_orchestra.infrastructure.files import relative_path

        if any(not arg or "\x00" in arg for arg in self.argv):
            raise ValueError("command arguments must not be empty or contain NUL")
        if self.working_directory != ".":
            relative_path(self.working_directory)
        if self.kind == "test" and not self.junit_report:
            raise ValueError("test commands require junit_report")
        if self.kind == "check" and self.junit_report is not None:
            raise ValueError("check commands must not declare junit_report")
        if self.junit_report:
            relative_path(self.junit_report)
        if any(x in self.argv for x in ["--passWithNoTests", "--collect-only"]):
            raise ValueError("test command must execute tests")
        return self


class Tests(Model):
    commands: dict[str, TestCommand] = Field(default_factory=dict)
    write_paths: list[str] = Field(default_factory=lambda: ["tests"])
    output_paths: list[str] = Field(default_factory=lambda: [".pytest_cache", "build", "target"])

    @model_validator(mode="after")
    def validate_tests(self):
        import re

        from dev_orchestra.infrastructure.files import relative_path

        for path in self.write_paths + self.output_paths:
            relative_path(path)
        if any(not re.fullmatch(r"[A-Za-z0-9_-]+", key) for key in self.commands):
            raise ValueError("test command IDs must be safe identifiers")
        if self.commands and not any(c.kind == "test" for c in self.commands.values()):
            raise ValueError(
                "at least one JUnit test command is required; checks alone are insufficient"
            )
        reports = [c.junit_report for c in self.commands.values() if c.junit_report]
        if len(reports) != len(set(reports)):
            raise ValueError("test commands must use distinct JUnit report paths")
        return self


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
