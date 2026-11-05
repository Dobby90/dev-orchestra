"""Codex exec adapter with read-only shells and role-scoped host tools."""

import json
import sys
import uuid
from pathlib import Path

from dev_orchestra.agents.contracts import ResponseError, response_fields
from dev_orchestra.domain.execution import writes_workspace
from dev_orchestra.domain.models import (
    AgentRequest,
    AgentResult,
    PipelineError,
    ReviewResultV2,
    WorkspaceResult,
    WorkspaceTestResult,
)
from dev_orchestra.infrastructure.agent_usage import UsageCollector
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.process import execute, execution_directory


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

    def check(self, stop=lambda: None):
        import tempfile

        with tempfile.TemporaryDirectory(prefix="devorchestra-check-") as temp:
            rc, output = execute(
                [self.config.command, "exec", "--help"],
                Path(temp),
                self.config.timeout_seconds,
                (
                    execution_directory() / "preflight" / (uuid.uuid4().hex + "-codex.log")
                    if execution_directory()
                    else Path(temp) / "check.log"
                ),
                stop=stop,
            )
            if rc or any(
                flag not in output
                for flag in ["--output-schema", "--ephemeral", "--ignore-user-config", "--json"]
            ):
                raise PipelineError("Codex CLI is unavailable or lacks required exec capabilities")

    def run_workspace(self, request, directory, session, stop=lambda: None):
        if not writes_workspace(request):
            raise PipelineError("workspace tools unavailable for this role")
        self.check_workspace(request, directory, session, stop)
        descriptor = directory / "workspace-launch.json"
        write(descriptor, {"session": str(session.path), "database": str(session.store.path)})
        return self.run(request, directory, stop, _workspace=descriptor)

    def run_readonly(self, request, directory, session, stop=lambda: None):
        if request.role != "reviewer":
            raise PipelineError("read-only evidence tools are restricted to reviewer")
        self.check_workspace(request, directory, session, stop)
        descriptor = directory / "review-evidence-launch.json"
        write(descriptor, {"session": str(session.path), "database": str(session.store.path)})
        return self.run(request, directory, stop, _workspace=descriptor)

    def check_workspace(self, request, directory, session, stop):
        """No model call: fail closed unless the installed CLI denies actual writes."""
        import tempfile

        with tempfile.TemporaryDirectory(prefix="devorchestra-policy-") as temp:
            name = ".devorchestra-probe-" + uuid.uuid4().hex
            roots = [
                Path(request.worktree),
                Path(session.data["repo"]),
                session.git.common(),
                session.path.parent,
                session.store.path.parent,
            ]
            for line in session.git.call("worktree", "list", "--porcelain").splitlines():
                if line.startswith("worktree "):
                    roots.append(Path(line.removeprefix("worktree ")))
            probes = [str(root / name) for root in roots]
            existing = [
                str(Path(request.worktree) / ".git"),
                str(session.git.common() / "config"),
                str(session.path),
                str(session.store.path),
            ]
            script = (
                "from pathlib import Path\nimport os\n"
                "for name in " + repr(probes) + ":\n"
                " try:\n  Path(name).write_text('probe')\n"
                " except PermissionError:\n  continue\n"
                " raise SystemExit('read-only boundary unavailable')\n"
                "for name in " + repr(existing) + ":\n"
                " try:\n  fd = os.open(name, os.O_WRONLY)\n"
                " except PermissionError:\n  continue\n"
                " os.close(fd)\n raise SystemExit('protected file boundary unavailable')\n"
            )
            try:
                rc, _ = execute(
                    [
                        self.config.command,
                        "sandbox",
                        "-P",
                        ":read-only",
                        "-C",
                        request.worktree,
                        sys.executable,
                        "-I",
                        "-c",
                        script,
                    ],
                    Path(temp),
                    30,
                    directory / "workspace-policy.log",
                    stop=stop,
                )
                if rc:
                    raise PipelineError("workspace read-only OS boundary could not be verified")
            finally:
                for probe in probes:
                    Path(probe).unlink(missing_ok=True)

    def run(self, request: AgentRequest, directory: Path, stop=lambda: None, *, _workspace=None):
        from dev_orchestra.agents.prompts import prompt

        directory.mkdir(parents=True, exist_ok=True)
        if writes_workspace(request) and _workspace is None:
            raise PipelineError("workspace contract requires validated host tools")
        schema = directory / "schema.json"
        result = directory / "response.json"
        model = (
            (WorkspaceTestResult if request.role == "tester" else WorkspaceResult)
            if writes_workspace(request)
            else (
                ReviewResultV2
                if request.role == "reviewer" and request.context.get("review_contract") == 2
                else AgentResult
            )
        )
        wire_schema = model.model_json_schema()
        fields = response_fields(request)
        wire_schema["properties"] = {
            k: v for k, v in wire_schema["properties"].items() if k in fields
        }
        if "no_change_reason" in fields:
            wire_schema["properties"]["no_change_reason"] = {"type": "string"}
        write(schema, strict_schema(wire_schema))
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
        if _workspace is not None:
            argv += ["--ignore-rules"]
            # Replace the MCP map so project/user servers cannot inherit write authority.
            argv += ["-c", "mcp_servers={}"]
            for key, value in {
                "command": sys.executable,
                "args": ["-I", "-m", "dev_orchestra.infrastructure.workspace_mcp", str(_workspace)],
                "required": True,
                "default_tools_approval_mode": "approve",
                "enabled_tools": (
                    ["edit_file", "test_policy", "run_tests", "read_test_evidence"]
                    if request.role == "tester"
                    else ["read_test_evidence"]
                    if request.role == "reviewer"
                    else ["edit_file", "checkpoint_commit"]
                ),
            }.items():
                argv += ["-c", "mcp_servers.workspace." + key + "=" + json.dumps(value)]
        options = self.config.for_role(request.role)
        if options.model:
            argv += ["--model", options.model]
        if options.reasoning_effort:
            argv += ["-c", f'model_reasoning_effort="{options.reasoning_effort}"']
        argv.append("-")
        collector = UsageCollector(directory / "telemetry.json", options)
        succeeded = False
        try:
            rc, _ = execute(
                argv,
                Path(request.worktree),
                self.config.role_timeouts.get(request.role, self.config.timeout_seconds),
                directory / "process.log",
                prompt(request),
                stop,
                on_event=collector.event,
                separate_stderr=True,
            )
            succeeded = rc == 0
        finally:
            collector.finish(succeeded)
        if rc:
            raise PipelineError(f"Codex {request.role} exited with code {rc}")
        if not result.exists():
            raise PipelineError("Codex returned no final JSON")
        try:
            raw = json.loads(result.read_text())
            required = set(model.model_fields)
            if not isinstance(raw, dict) or set(raw) not in (required, fields):
                raise ValueError("incomplete role response")
            if "no_change_reason" in raw and model not in {WorkspaceResult, WorkspaceTestResult}:
                reason = raw.pop("no_change_reason")
                if not isinstance(reason, str):
                    raise ValueError("no_change_reason must be a string")
                if not raw.get("patch") and not raw.get("questions"):
                    if not reason.strip():
                        raise ValueError("empty patch requires no_change_reason")
                    raw["summary"] += "\nNo change: " + reason
            validated = model.model_validate(raw, strict=True)
            from dev_orchestra.infrastructure.process import sanitize

            write(result, sanitize(validated.model_dump()))
            return validated
        except (ValueError, OSError) as error:
            from dev_orchestra.infrastructure.process import redact

            write(result, redact(result.read_text()))
            raise ResponseError(
                "invalid Codex result schema; return every required role field: "
                + ", ".join(sorted(fields))
            ) from error
