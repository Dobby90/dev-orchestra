"""Read-only role/checkpoint and capacity presentation; no recovery on queries."""

import json
from pathlib import Path

from dev_orchestra.application.results import Results
from dev_orchestra.application.workspace_recovery import checkpoint_data
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.coordination import database_root, policy, repository_root
from dev_orchestra.infrastructure.process import sanitize
from dev_orchestra.infrastructure.process_owner import alive


def role_progress(store, run, step):
    value = {"commits": [], "preserved_changes": 0, "recovery": None, "checkpoint_sha": None}
    try:
        if step["status"] == "completed":
            data = Results(store).payload(run, step)
            commits = data.get("workspace_commits", [])
        else:
            checkpoint = checkpoint_data(store, run)
            if not checkpoint or checkpoint[1]["step_id"] != step["id"]:
                return value
            _, data = checkpoint
            commits = data["commits"]
            value.update(
                preserved_changes=sum(
                    item != data.get("initial", {}).get(path)
                    for path, item in data["files"].items()
                ),
                checkpoint_sha=data["current_sha"],
                recovery="metadata" if data.get("metadata_only") else "same_role",
            )
        value["commits"] = [{"sha": c["sha"], "summary": sanitize(c["summary"])} for c in commits]
    except (PipelineError, OSError, KeyError, ValueError, TypeError):
        value["recovery"] = "unavailable"
    return value


def capacity_view(store, run):
    result = {
        "active": 0,
        "total": 1,
        "repository_active": 0,
        "repository_limit": 1,
        "wait_reason": None,
    }
    try:
        result["total"] = policy(database_root(store.path))

        def owners(root):
            return {
                record.get("run_directory", record["directory"])
                for path in root.glob("owner-*.json")
                for record in [json.loads(path.read_text())]
            }

        database_owners = owners(database_root(store.path))
        result["active"] = len(database_owners)
        common, key = repository_root(run["repo"])
        root = common / "devorchestra-capacity-v1"
        result["repository_limit"] = policy(root)
        # Durable records include unconfirmed children. Count conservatively, never reclaim here.
        repository_owners = owners(root)
        result["repository_active"] = len(repository_owners)
        with store.connection() as db:
            if db.execute("SELECT version FROM schema_version").fetchone()[0] >= 5:
                active = db.execute(
                    "SELECT run_id FROM jobs WHERE state IN ('claimed','running')"
                ).fetchall()
                for row in active:
                    candidate = store.get(row[0])
                    database_owners.add(candidate["directory"])
                    if repository_root(candidate["repo"])[1] == key:
                        repository_owners.add(candidate["directory"])
                result["active"] = len(database_owners)
                result["repository_active"] = len(repository_owners)
        if run["state"] == "queued" and run["directory"] not in database_owners:
            if result["active"] >= result["total"]:
                result["wait_reason"] = "total_capacity"
            elif result["repository_active"] >= result["repository_limit"]:
                result["wait_reason"] = "repository_capacity"
        marker = Path(run["directory"]) / "capacity-wait.json"
        if marker.is_file() and run["state"] in {
            "queued",
            "preparing",
            "developing",
            "testing",
            "fixing",
        }:
            data = json.loads(marker.read_text())
            if data.get("reason") in {
                "total_capacity",
                "repository_capacity",
                "shared_resource",
            } and alive(data["owner"]):
                result["wait_reason"] = data["reason"]
    except (PipelineError, OSError, KeyError, ValueError, TypeError):
        result["wait_reason"] = "unknown"
    return result
