"""Explicit CLI session ownership and per-call accounting, separate from source state."""

import json
import uuid
from pathlib import Path

from dev_orchestra.application.test_changes import checksum
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.agent_usage import AgentTelemetry, TokenUsage
from dev_orchestra.infrastructure.files import contained, digest, write


class ConversationUnavailable(PipelineError):
    pass


def binding(session, options):
    return {
        key: session.data[key]
        for key in [
            "run_id",
            "role",
            "step_id",
            "revision",
            "iteration",
            "input_sha",
            "worktree",
            "policy_hash",
            "task_hash",
        ]
    } | {"options_hash": checksum(options.model_dump())}


def session_id(session, options):
    reference = (
        session.store.get(session.run_id)
        .get("codex_conversations", {})
        .get(str(session.data["step_id"]))
    )
    if not reference:
        return None
    try:
        path = contained(Path(session.store.get(session.run_id)["directory"]), reference["path"])
        raw = path.read_bytes()
        if digest(raw) != reference["hash"]:
            return None  # Conversation loss never invalidates verified source checkpoints.
        record = json.loads(raw)
        if (
            record["binding"]["run_id"] != session.run_id
            or record["binding"]["step_id"] != session.data["step_id"]
        ):
            raise PipelineError("Codex session belongs to another Run or stage")
        if record["binding"] != binding(session, options) or record.get("invalid"):
            return None
        return str(uuid.UUID(record["session_id"]))
    except (ValueError, OSError, KeyError, TypeError):
        return None


def thread_event(session, options, expected=None):
    observed = []

    def event(value):
        if value.get("type") != "thread.started":
            return
        try:
            thread = str(uuid.UUID(value["thread_id"]))
        except (ValueError, KeyError, TypeError) as error:
            raise PipelineError("Codex returned an invalid session identity") from error
        if (expected and thread != expected) or (observed and thread != observed[0]):
            raise PipelineError("Codex resumed a different session; files preserved")
        observed.append(thread)
        path = session.path.parent / "conversation.json"
        sha = write(path, {"session_id": thread, "binding": binding(session, options)})
        run = session.store.get(session.run_id)
        references = run.get("codex_conversations", {})
        references[str(session.data["step_id"])] = {
            "path": str(path.relative_to(Path(run["directory"]))),
            "hash": sha,
        }
        session.store.update(session.run_id, codex_conversations=references)

    return event


def next_call(directory):
    calls = directory / "calls"
    calls.mkdir(parents=True, exist_ok=True)
    count = max([int(path.name.split("-")[1]) for path in calls.glob("call-*")], default=0) + 1
    path = calls / f"call-{count:03}"
    path.mkdir()
    return path


def aggregate_calls(directory, options):
    paths = sorted((directory / "calls").glob("call-*"))
    values = []
    for path in paths:
        try:
            values.append(
                AgentTelemetry.model_validate_json((path / "telemetry.json").read_bytes())
            )
        except (ValueError, OSError):
            values.append(AgentTelemetry(**options.model_dump()))
    measured = [value for value in values if value.usage is not None]
    result = AgentTelemetry(
        **options.model_dump(),
        completed_turns=sum(value.completed_turns for value in values),
        duration_seconds=sum(value.duration_seconds or 0 for value in values),
        usage=TokenUsage(
            **{
                key: sum(getattr(value.usage, key) for value in measured)
                for key in TokenUsage.model_fields
            }
        )
        if measured
        else None,
        usage_status="reported"
        if values and all(value.usage_status == "reported" for value in values)
        else "partial"
        if measured
        else "unknown",
    )
    write(directory / "telemetry.json", result.model_dump())
