"""Human decisions bound to immutable planner checkpoints."""

import json
from pathlib import Path

from dev_orchestra.application.results import Results
from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process import sanitize
from dev_orchestra.infrastructure.process_owner import ensure_stopped
from dev_orchestra.infrastructure.store import now


def binding(store, run):
    steps = [
        s
        for s in store.steps(run["id"])
        if s["role"] == "planner"
        and s["status"] == "completed"
        and s.get("revision", 0) == run.get("revision", 0)
    ]
    if not steps:
        raise PipelineError("completed plan required")
    step = steps[-1]
    payload = Results(store).payload(run, step)
    if payload["plan"] != run.get("plan") or payload["criteria"] != run.get("criteria"):
        raise PipelineError("plan differs from checkpoint")
    return {
        "run_id": run["id"],
        "revision": run.get("revision", 0),
        "step_id": step["id"],
        "input_sha": step["output_sha"],
        "plan_hash": digest(
            json.dumps(
                {
                    "run_id": run["id"],
                    "revision": run.get("revision", 0),
                    "sha": step["output_sha"],
                    "artifact_hash": step["data"]["hash"],
                }
                | {
                    k: run.get(k)
                    for k in ["plan", "criteria", "spec", "answers", "clarifications", "directives"]
                },
                sort_keys=True,
            ).encode()
        ),
        "artifact_hash": step["data"]["hash"],
    }


def verify_development(store, run):
    expected = binding(store, run)
    approval = run.get("plan_approval") or {}
    if any(approval.get(k) != v for k, v in expected.items()):
        raise PipelineError("human plan approval required for current plan")


def decision_changes(store, run, plan_hash, approve, reason=""):
    if run["state"] != "waiting_plan_approval":
        raise PipelineError("plan decision requires waiting_plan_approval")
    evidence = binding(store, run)
    if evidence["plan_hash"] != plan_hash:
        raise PipelineError("plan changed; refresh before deciding")
    git = Git(Path(run["worktree"]), Config.model_validate(run["config"]).tests.output_paths)
    if git.dirty() or git.sha() != evidence["input_sha"]:
        raise PipelineError("plan decision requires unchanged HEAD and clean worktree")
    if approve:
        return {"plan_approval": evidence | {"approved_at": now()}, "next_role": "developer"}
    if not reason.strip() or len(reason.encode()) > 65536:
        raise PipelineError("plan revision instructions required within 64 KiB")
    return {
        "plan_approval": None,
        "revision": run.get("revision", 0) + 1,
        "next_role": "planner",
        "plan": None,
        "criteria": [],
        "directives": [*run.get("directives", []), sanitize(reason)],
    }


def decide(service, run_id, plan_hash, approve, reason="", expected_version=None):
    run = service.store.get(run_id)
    with lock(Path(run["directory"]) / "execution.lock"):
        run = service.store.get(run_id)
        ensure_stopped(Path(run["directory"]))
        changes = decision_changes(service.store, run, plan_hash, approve, reason)
        with service.store.transaction() as db:
            service.commit_resume(
                db, run, changes, run["version"] if expected_version is None else expected_version
            )
            service.store._event(
                db, run_id, "plan.decided", {"approved": approve, "plan_hash": plan_hash}
            )
    service.refresh_report(run_id)
