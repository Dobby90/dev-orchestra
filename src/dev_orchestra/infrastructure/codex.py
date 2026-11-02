"""Codex exec adapter. Fresh read-only sessions return validated patches."""

import subprocess
from pathlib import Path

from dev_orchestra.domain.models import AgentRequest, AgentResult, PipelineError
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.process import execute


def strict_schema(value):
    if isinstance(value, dict):
        value = {k: strict_schema(v) for k, v in value.items() if k != "default"}
        if value.get("type") == "object":
            value["required"] = list(value.get("properties", {}))
            value["additionalProperties"] = False
    elif isinstance(value, list):
        value = [strict_schema(v) for v in value]
    return value


class CodexClient:
    def __init__(self, config):
        self.config = config

    def check(self):
        p = subprocess.run([self.config.command, "exec", "--help"], capture_output=True, text=True)
        if p.returncode or any(
            flag not in p.stdout
            for flag in ["--output-schema", "--ephemeral", "--ignore-user-config", "--json"]
        ):
            raise PipelineError("Codex CLI is unavailable or lacks required exec capabilities")

    def run(self, request: AgentRequest, directory: Path, stop=lambda: None):
        from dev_orchestra.agents.prompts import prompt

        directory.mkdir(parents=True, exist_ok=True)
        schema = directory / "schema.json"
        result = directory / "response.json"
        write(schema, strict_schema(AgentResult.model_json_schema()))
        argv = [
            self.config.command,
            "-a",
            "never",
            "exec",
            "--cd",
            request.worktree,
            "--sandbox",
            "read-only",
            "--ignore-user-config",
            "--ephemeral",
            "--json",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(result),
        ]
        if self.config.model:
            argv += ["--model", self.config.model]
        argv.append("-")
        rc, _ = execute(
            argv,
            Path(request.worktree),
            self.config.role_timeouts.get(request.role, self.config.timeout_seconds),
            directory / "process.log",
            prompt(request),
            stop,
        )
        if rc:
            raise PipelineError(f"Codex {request.role} exited with code {rc}")
        if not result.exists():
            raise PipelineError("Codex returned no final JSON")
        try:
            return AgentResult.model_validate_json(result.read_text())
        except (ValueError, OSError) as error:
            raise PipelineError("invalid Codex result schema") from error
