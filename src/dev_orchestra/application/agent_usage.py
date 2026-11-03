"""Read-only per-attempt telemetry; never sums replayed SSE log messages."""

from pathlib import Path

from dev_orchestra.config import ROLES, Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.agent_usage import AgentTelemetry, TokenUsage
from dev_orchestra.infrastructure.files import contained


def usage_view(store, run_id):
    run = store.get(run_id)
    config = Config.model_validate(run["config"]).codex
    attempts = []
    for step in store.steps(run_id):
        if step["role"] not in ROLES:
            continue
        telemetry = AgentTelemetry(**config.for_role(step["role"]).model_dump())
        relative = (
            f"iterations/{step['iteration']:03}/{step['role']}/"
            f"attempt-{step['attempt']:03}/telemetry.json"
        )
        try:
            path = contained(Path(run["directory"]), relative)
            with path.open("rb") as handle:
                raw = handle.read(65537)
            if len(raw) <= 65536:
                telemetry = AgentTelemetry.model_validate_json(raw)
        except (OSError, ValueError, PipelineError):
            pass
        attempts.append(
            {
                "step_id": step["id"],
                "role": step["role"],
                "iteration": step["iteration"],
                "attempt": step["attempt"],
                "revision": step.get("revision", 0),
                "status": step["status"],
                **telemetry.model_dump(),
            }
        )
    measured = [a for a in attempts if a["usage"] is not None]
    total = (
        {key: sum(a["usage"][key] for a in measured) for key in TokenUsage.model_fields}
        if measured
        else None
    )
    return {
        "agents": {role: config.for_role(role).model_dump() for role in ROLES},
        "attempts": attempts,
        "usage": total,
        "usage_status": (
            "reported"
            if attempts and all(a["usage_status"] == "reported" for a in attempts)
            else "partial"
            if measured
            else "unknown"
        ),
        "measured_attempts": len(measured),
        "total_attempts": len(attempts),
    }
