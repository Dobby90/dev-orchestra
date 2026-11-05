"""Small stdio MCP adapter exposing only host-validated workspace operations.

Launched by Codex outside its read-only shell sandbox. Task paths and database
identity come from a private host descriptor, never from tool arguments.
"""

import json
import sys

from pydantic import Field, ValidationError

from dev_orchestra.domain.models import Model, PipelineError, TestChange, TestTarget
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.process import execution_scope, redact
from dev_orchestra.infrastructure.workspace import load_session


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
    command_ids: list[str]
    test_plan: list[TestTarget] | None = None


class Empty(Model):
    pass


class ReadEvidence(Model):
    evidence_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8000, ge=1, le=8000)


TOOLS = {
    "edit_file": (
        EditFile,
        "Write one UTF-8 file now. Supply SHA256 of its current bytes, or null for a new file. Existing tests require file/criterion_ids/reason evidence.",
    ),
    "checkpoint_commit": (
        CheckpointCommit,
        "Commit a meaningful work unit in Korean. Supply a unique stable request_id, current HEAD, and exactly selected paths mapped to SHA256. Safe to retry identical requests.",
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
        "Commit pending test edits and run host-validated commands. Automatic selection requires test_plan (command_id, exact files relative to command working_directory, reason); command_ids must include mandatory commands. Inspect returned evidence.",
    ),
    "read_test_evidence": (
        ReadEvidence,
        "Read a bounded slice of this tester attempt host evidence.",
    ),
}


def _dispatch(session, request):
    if session.data["role"] != "reviewer":
        session.reload()
    catalog = (
        {"read_test_evidence": TEST_TOOLS["read_test_evidence"]}
        if session.data["role"] == "reviewer"
        else TEST_TOOLS
        if session.data["role"] == "tester"
        else TOOLS
    )
    if session.data["role"] != "reviewer":
        catalog = {
            **catalog,
            "workspace_state": (
                Empty,
                "Read current source hashes, confirmed commits and preserved test evidence; never writes files.",
            ),
        }
        if session.data.get("metadata_only"):
            catalog = {
                key: value
                for key, value in catalog.items()
                if key in {"workspace_state", "test_policy", "read_test_evidence"}
            }
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


def dispatch(session, request):
    result = _dispatch(session, request)
    if request.get("method") == "tools/call":
        try:
            # Observation only: counts include retries and errors, not just successful tools.
            path = session.path.parent / "tool-metrics.json"
            values = json.loads(path.read_text()) if path.exists() else {}
            name = request["params"]["name"]
            entry = values.setdefault(name, {"calls": 0, "argument_bytes": 0, "result_bytes": 0})
            entry["calls"] += 1
            entry["argument_bytes"] += len(
                json.dumps(request["params"].get("arguments", {}), ensure_ascii=False).encode(
                    "utf-8"
                )
            )
            entry["result_bytes"] += len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
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
