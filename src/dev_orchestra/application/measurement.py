"""Read-only observations for reproducible offline comparisons."""

import json
import math
from datetime import datetime
from pathlib import Path, PurePosixPath

from dev_orchestra.application.agent_usage import summarize_usage, usage_view
from dev_orchestra.application.automatic_tests import AUTOMATIC_TEST_CONTRACTS, automatic
from dev_orchestra.application.results import Results
from dev_orchestra.application.test_changes import checksum
from dev_orchestra.application.test_selection import SELECTOR_VERSION
from dev_orchestra.application.validation import run_test_policy, run_test_policy_hash
from dev_orchestra.domain.execution import WORKSPACE_V2
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained, relative_path

COMPARISON_KEY_VERSION = 2


def _read(root, relative):
    try:
        return json.loads(contained(root, relative).read_text())
    except (OSError, ValueError, PipelineError):
        return None


def _elapsed(start, finish):
    if not start or not finish:
        return None
    try:
        value = (datetime.fromisoformat(finish) - datetime.fromisoformat(start)).total_seconds()
        return value if value >= 0 else None
    except (ValueError, TypeError):
        return None


def _total_seconds(values):
    # A missing duration is different from a measured zero, including on failed attempts.
    if values and all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values):
        return sum(values)
    return None


def _role_summary(stages, usage):
    rows = []
    for role in usage["agents"]:
        attempts = [a for a in usage["attempts"] if a["role"] == role]
        if not attempts:
            continue
        steps = [s for s in stages if s["role"] == role]
        durations = [call["duration_seconds"] for s in steps for call in s["calls"]]
        cli_seconds = _total_seconds(durations) if all(s["calls"] for s in steps) else None
        summary = summarize_usage(attempts)
        tokens = summary["usage"]
        rows.append(
            {
                "role": role,
                "wall_seconds": _total_seconds([s["wall_seconds"] for s in steps]),
                "cli_seconds": cli_seconds,
                **summary,
                "uncached_input_tokens": tokens["input_tokens"] - tokens["cached_input_tokens"]
                if tokens is not None
                else None,
                "total_tokens": tokens["input_tokens"] + tokens["output_tokens"]
                if tokens is not None
                else None,
            }
        )
    return rows


def _command_signature(command):
    cwd = command["working_directory"]
    return command["argv"], "." if cwd == "." else relative_path(cwd), command["kind"]


def _test_workload(run, step, result, invocations):
    selection = result["test_selection"]
    selector = run["test_selection_contract"] if automatic(run) else SELECTOR_VERSION
    revision = step.get("revision", 0)
    if (
        selection["selector_version"] != selector
        or selection["requested_scope"] != run.get("test_scope", "full")
        or selection["actual_scope"] not in {"full", "affected"}
        or selection["base_sha"] != run["base_sha"]
        or result.get("revision", 0) != revision
        or selection["revision"] != revision
        or selection["candidate_sha"] != result["sha"]
        or selection["selection_hash"]
        != checksum({key: val for key, val in selection.items() if key != "selection_hash"})
    ):
        return None
    included = selection["included"]
    if selector in AUTOMATIC_TEST_CONTRACTS:
        commands = selection["resolved_commands"]
    else:
        stage_run = run | {"revision": revision, "iteration": step["iteration"]}
        policy = run_test_policy(stage_run)
        if selection["policy_hash"] != run_test_policy_hash(stage_run):
            return None
        commands = {key: policy.commands[key].model_dump() for key in included}

    execution = result["execution"]
    actual = execution["commands"]
    command_order = [item["command_id"] for item in actual]
    if (
        not included
        or set(included) != commands.keys()
        or len(included) != len(commands)
        or set(command_order) != commands.keys()
        or len(command_order) != len(commands)
        or selection["commands_hash"] != checksum(commands)
        or execution["status"] not in {"passed", "failed"}
        or invocations[0]["status"] != execution["status"]
        or [item["command_id"] for item in invocations[0]["commands"]] != command_order
        or any(
            item["status"] not in {"passed", "failed"}
            or _command_signature(item) != _command_signature(commands[item["command_id"]])
            for item in actual
        )
    ):
        return None

    # All selectors hash the resolved argv and working directory. Gradle can translate
    # selected files to the same class filters, so retain canonical file identities too.
    targets = {
        item["command_id"]: sorted(
            str(
                PurePosixPath(
                    commands[item["command_id"]]["working_directory"], relative_path(path)
                )
            )
            for path in item["files"]
        )
        for item in selection.get("test_plan", [])
        if item["files"]
    }
    return {
        "actual_scope": selection["actual_scope"],
        "commands_hash": selection["commands_hash"],
        "command_order": command_order,
        "targets": targets,
    }


def _comparison_key(store, run, steps, stages, conditions, scope):
    cached = run.get("test_result") or {}
    if (
        run.get("execution_contract") != WORKSPACE_V2
        or run.get("test_selection_contract")
        not in {None, SELECTOR_VERSION, *AUTOMATIC_TEST_CONTRACTS}
        or not cached.get("sha")
        or cached["sha"] != run.get("current_sha")
        or cached.get("revision", 0) != run.get("revision", 0)
    ):
        return None
    workloads = []
    latest = None
    results = Results(store)
    try:
        for step, stage in zip(steps, stages, strict=True):
            if step["role"] != "tester":
                continue
            invocations = stage["test_invocations"]
            # One finalized result proves one invocation. Additional or interrupted
            # invocations lack complete per-command workload evidence.
            if step["status"] != "completed" or not invocations or len(invocations) != 1:
                return None
            latest = results.payload(run, step)
            workload = _test_workload(run, step, latest, invocations)
            if workload is None:
                return None
            workloads.append(workload)
    except (PipelineError, KeyError, OSError, TypeError, ValueError):
        return None
    if latest != cached:
        return None
    return checksum(
        {
            "version": COMPARISON_KEY_VERSION,
            "conditions": conditions,
            "test_scope": scope,
            "test_workloads": workloads,
        }
    )


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
    steps = store.steps(run_id)
    for step in steps:
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
        "execution_contract": run.get("execution_contract", "unknown"),
        "test_scope": scope,
        "conditions": conditions,
        "validation": validation,
        "same_scope_comparison_version": COMPARISON_KEY_VERSION,
        "same_scope_comparison_key": _comparison_key(store, run, steps, stages, conditions, scope),
        "usage": (
            {**total, "uncached_input_tokens": total["input_tokens"] - total["cached_input_tokens"]}
            if total is not None
            else None
        ),
        "usage_status": usage["usage_status"],
        "observed_role_calls": sum(len(stage["calls"]) for stage in stages),
        "role_summary": _role_summary(stages, usage),
        "stages": stages,
    }
