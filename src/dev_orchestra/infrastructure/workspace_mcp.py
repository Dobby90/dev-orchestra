"""Small stdio MCP adapter exposing only host-validated workspace operations.

Launched by Codex outside its read-only shell sandbox. Task paths and database
identity come from a private host descriptor, never from tool arguments.
"""

import json
import sys
import time
from typing import Annotated, Literal

from pydantic import Field, ValidationError

from dev_orchestra.domain.models import Model, PipelineError, TestChange, TestTarget
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.process import execution_scope, redact
from dev_orchestra.infrastructure.workspace import MAX_HASH_PATHS, load_session

MAX_TRACKED_EVIDENCE_PAGES = 128


class EditFile(Model):
    file: str
    expected_hash: str | None
    content: str
    test_change: TestChange | None = None


class CheckpointCommit(Model):
    request_id: str
    expected_sha: str
    files: dict[str, str] = Field(min_length=1)
    message: str
    summary: str


class RunTests(Model):
    commit_message: str = ""
    command_ids: list[str]
    test_plan: list[TestTarget] | None = None


class Empty(Model):
    pass


class ReadEvidence(Model):
    evidence_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8000, ge=1, le=8000)


class ReadCommandEvidence(ReadEvidence):
    command_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    view: Literal["diagnostics", "test_cases"] = "diagnostics"


class WorkspaceState(Model):
    paths: list[Annotated[str, Field(min_length=1, max_length=4096)]] | None = Field(
        default=None, min_length=1, max_length=MAX_HASH_PATHS
    )
    summary_only: bool = False


READ_EVIDENCE = (
    ReadCommandEvidence,
    "Read current candidate evidence. Pass command_id for one command's status and diagnostics. "
    "Use view=test_cases with command_id for executed names from verified JUnit. Omit command_id "
    "for full coverage and test selection. Continue with offset when truncated.",
)


TOOLS = {
    "prepare_environment": (
        Empty,
        "Prepare the declared devorchestra.environment.json Docker toolchain and project dependencies. The host installs tools without project source, disables repository install hooks, and writes genuinely generated new lockfiles through the editor. Does not execute tests or change validation policy. Call after writing manifests and before checkpointing lockfiles.",
    ),
    "edit_file": (
        EditFile,
        "Write one UTF-8 file now. Supply SHA256 of its current bytes, or null for a new file. Existing tests require file/criterion_ids/reason evidence.",
    ),
    "checkpoint_commit": (
        CheckpointCommit,
        "Commit a meaningful work unit with a concrete Korean subject and a body explaining only the selected changes and why. Supply a unique stable request_id, current HEAD, and exactly selected paths mapped to SHA256. Safe to retry identical requests.",
    ),
}


TEST_TOOLS = {
    "edit_file": TOOLS["edit_file"],
    "test_policy": (
        Empty,
        "Read current test policy and available commands. For automatic selection analyze the cumulative changes and propose test_plan.",
    ),
    "run_tests": (
        RunTests,
        "Commit pending test edits and run host-validated commands. Supply commit_message with a Korean subject naming the tested behavior and a body describing only pending test changes; never claim unexecuted tests passed. Automatic selection requires test_plan (command_id, exact files relative to command working_directory, reason); command_ids must include mandatory commands. Inspect returned evidence.",
    ),
    "read_test_evidence": READ_EVIDENCE,
}


def workspace_tools(role, metadata_only=False):
    if role == "reviewer":
        return {"read_test_evidence": READ_EVIDENCE}
    if role == "planner":
        return {}
    tester = role == "tester"
    catalog = {
        **(TEST_TOOLS if tester else TOOLS),
        "workspace_state": (
            WorkspaceState,
            "Set summary_only=true for candidate SHA and checkpoint metadata without file hashes. "
            f"For edits, pass only needed repo-relative paths (up to {MAX_HASH_PATHS}) for "
            "candidate SHA and current hashes (null for missing files). Use one option at a time. "
            "An empty argument object returns full state for legacy callers.",
        ),
    }
    if metadata_only:
        return {
            key: value
            for key, value in catalog.items()
            if key in {"workspace_state", "test_policy", "read_test_evidence"}
        }
    return catalog


def _dispatch(session, request):
    if session.data["role"] != "reviewer":
        session.reload()
    catalog = workspace_tools(session.data["role"], session.data.get("metadata_only", False))
    method = request.get("method")
    if method == "initialize":
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "devorchestra-workspace", "version": "1.0.0"},
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {
            "tools": [
                {"name": name, "description": description, "inputSchema": model.model_json_schema()}
                for name, (model, description) in catalog.items()
            ]
        }
    if method != "tools/call":
        raise PipelineError("unsupported workspace MCP method")
    params = request["params"]
    name = params["name"]
    if name not in catalog:
        raise PipelineError("unsupported workspace tool")
    try:
        args = catalog[name][0].model_validate(params.get("arguments", {}), strict=True)
        with execution_scope(session.path.parent):
            result = getattr(session, name)(**args.model_dump())
        return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}
    except (PipelineError, ValidationError, OSError, ValueError, TypeError) as error:
        # Do not include invalid input values in model-visible validation errors.
        message = (
            "invalid workspace tool arguments"
            if isinstance(error, ValidationError)
            else redact(str(error))
        )
        return {"isError": True, "content": [{"type": "text", "text": message}]}


def observe_evidence_read(entry, arguments, failed, elapsed):
    entry["duration_seconds"] = entry.get("duration_seconds", 0) + elapsed
    entry["failed_calls"] = entry.get("failed_calls", 0) + int(failed)
    if failed:
        return
    # Successful reads have passed the host's evidence and command checks.
    # Normalize omitted defaults so equivalent requests share a counter.
    target = ReadCommandEvidence.model_validate(arguments, strict=True).model_dump()
    pages = entry.setdefault("pages", [])
    for page in pages:
        if page["target"] == target:
            page["calls"] += 1
            page["duration_seconds"] += elapsed
            return
    if len(pages) < MAX_TRACKED_EVIDENCE_PAGES:
        pages.append({"target": target, "calls": 1, "duration_seconds": elapsed})
    else:
        entry["untracked_page_reads"] = entry.get("untracked_page_reads", 0) + 1


def dispatch(session, request):
    started = time.monotonic()
    result = _dispatch(session, request)
    elapsed = time.monotonic() - started
    if request.get("method") == "tools/call":
        try:
            # Observation only: counts include retries and errors, not just successful tools.
            path = session.path.parent / "tool-metrics.json"
            values = json.loads(path.read_text()) if path.exists() else {}
            params = request["params"]
            name = params["name"]
            arguments = params.get("arguments", {})
            entry = values.setdefault(name, {"calls": 0, "argument_bytes": 0, "result_bytes": 0})
            entry["calls"] += 1
            entry["argument_bytes"] += len(
                json.dumps(arguments, ensure_ascii=False).encode("utf-8")
            )
            entry["result_bytes"] += len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            if name == "read_test_evidence" and session.data["role"] == "tester":
                observe_evidence_read(entry, arguments, result.get("isError", False), elapsed)
            write(path, values)
        except (OSError, ValueError, TypeError, KeyError):
            pass  # Metrics never change the completed host operation or its evidence.
    return result


def main():
    session = load_session(sys.argv[1])
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        try:
            response = {"jsonrpc": "2.0", "id": request["id"], "result": dispatch(session, request)}
        except (PipelineError, KeyError, TypeError) as error:
            response = {
                "jsonrpc": "2.0",
                "id": request["id"],
                "error": {"code": -32601, "message": redact(str(error))},
            }
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
