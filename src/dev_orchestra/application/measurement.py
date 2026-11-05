"""Read-only, size-only observations for reproducible offline comparisons."""

import json
from datetime import datetime
from pathlib import Path

from dev_orchestra.application.agent_usage import usage_view
from dev_orchestra.application.test_changes import checksum
from dev_orchestra.infrastructure.files import contained


def _read(root, relative):
    try:
        return json.loads(contained(root, relative).read_text())
    except (OSError, ValueError):
        return None


def _elapsed(start, finish):
    if not start or not finish:
        return None
    try:
        value = (datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds()
        return value if value >= 0 else None
    except (ValueError, TypeError):
        return None


def measurement(store, run_id, *, version_label, scenario):
    run = store.get(run_id)
    root = Path(run["directory"])
    usage = usage_view(store, run_id)
    config = run["config"]
    conditions = {
        "spec_hash": run["spec_hash"],
        "base_sha": run["base_sha"],
        "agents": usage["agents"],
        "tests_hash": checksum(config["tests"]),
        "workflow_hash": checksum(config["workflow"]),
        "test_selection_contract": run.get("test_selection_contract"),
        "review_contract": run.get("review_contract", 1),
        "criteria_hash": checksum(run.get("criteria", [])),
    }
    with store.connection() as db:
        events = [
            dict(row)
            for row in db.execute(
                "SELECT timestamp,kind,json_extract(data, '$.step_id') AS step_id FROM events "
                "WHERE run_id=? AND kind IN ('step_started','step_resumed','step_completed','step.interrupted') "
                "ORDER BY id",
                (run_id,),
            )
        ]
    stages = []
    for step in store.steps(run_id):
        relative = f"iterations/{step['iteration']:03}/{step['role']}/attempt-{step['attempt']:03}"
        directory = contained(root, relative)
        calls = sorted((directory / "calls").glob("call-*"))
        # Workspace calls have their own telemetry; do not add the parent aggregate again.
        if not calls and (directory / "telemetry.json").exists():
            calls = [directory]
        observations = []
        for call in calls:
            path = str(call.relative_to(root))
            telemetry = _read(root, path + "/telemetry.json")
            observations.append(
                {
                    "input": _read(root, path + "/input-metrics.json"),
                    "duration_seconds": telemetry.get("duration_seconds") if telemetry else None,
                    "usage_status": telemetry.get("usage_status", "unknown")
                    if telemetry
                    else "unknown",
                }
            )
        test_invocations = [
            _read(root, str(path.relative_to(root)))
            for path in sorted(directory.glob("**/test-invocation-*.json"))
        ]
        timeline = [event for event in events if event["step_id"] == step["id"]]
        started_at = step.get("started_at") or next(
            (event["timestamp"] for event in timeline if event["kind"] == "step_started"), None
        )
        finished_at = step.get("finished_at") or next(
            (
                event["timestamp"]
                for event in reversed(timeline)
                if event["kind"] == "step_completed"
            ),
            None,
        )
        resumed_at = next(
            (event["timestamp"] for event in reversed(timeline) if event["kind"] == "step_resumed"),
            None,
        )
        stages.append(
            {
                "role": step["role"],
                "iteration": step["iteration"],
                "attempt": step["attempt"],
                "status": step["status"],
                "input_sha": step["input_sha"],
                "output_sha": step.get("output_sha"),
                "started_at": started_at,
                "finished_at": finished_at,
                "timeline": timeline,
                "wall_seconds": _elapsed(started_at, finished_at),
                "latest_resume_seconds": _elapsed(resumed_at, finished_at),
                "calls": observations,
                "tools": _read(root, relative + "/tool-metrics.json"),
                "test_invocations": test_invocations or None,
            }
        )
    total = usage["usage"]
    scope = run.get("test_scope", "full")
    selection = (run.get("test_result") or {}).get("test_selection")
    validation = (
        {
            key: selection.get(key)
            for key in [
                "requested_scope",
                "actual_scope",
                "included",
                "excluded",
                "expanded",
                "reasons",
                "candidate_sha",
                "revision",
                "policy_hash",
                "criteria_hash",
                "selection_hash",
            ]
        }
        if selection
        else None
    )
    return {
        "version_label": version_label,
        "scenario": scenario,
        "state": run["state"],
        "execution_contract": run.get("execution_contract", "patch-v1"),
        "test_scope": scope,
        "conditions": conditions,
        "validation": validation,
        "same_scope_comparison_key": checksum(
            {
                **conditions,
                "test_scope": scope,
                "actual_scope": selection.get("actual_scope") if selection else None,
                "required_commands": selection.get("included") if selection else None,
            }
        ),
        "usage": (
            {**total, "uncached_input_tokens": total["input_tokens"] - total["cached_input_tokens"]}
            if total is not None
            else None
        ),
        "usage_status": usage["usage_status"],
        "observed_role_calls": sum(len(stage["calls"]) for stage in stages),
        "stages": stages,
    }
