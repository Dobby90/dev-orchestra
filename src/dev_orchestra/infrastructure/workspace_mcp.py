"""Small stdio MCP adapter exposing only host-validated workspace operations.

Launched by Codex outside its read-only shell sandbox. Task paths and database
identity come from a private host descriptor, never from tool arguments.
"""

import json
import sys

from pydantic import Field, ValidationError

from dev_orchestra.domain.models import Model, PipelineError, TestChange
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


def dispatch(session, request):
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
                for name, (model, description) in TOOLS.items()
            ]
        }
    if method != "tools/call":
        raise PipelineError("unsupported workspace MCP method")
    params = request["params"]
    name = params["name"]
    if name not in TOOLS:
        raise PipelineError("unsupported workspace tool")
    try:
        args = TOOLS[name][0].model_validate(params.get("arguments", {}), strict=True)
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
