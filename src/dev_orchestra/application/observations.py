"""Read-only execution/validation observations. Never repairs state on GET."""

import time
from pathlib import Path

from dev_orchestra.application.handoff import handoff
from dev_orchestra.application.progress import capacity_view, role_progress
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.evidence import verified_candidate
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.process_owner import alive, markers
from dev_orchestra.infrastructure.store import now


def observe(store, run):
    execution = {
        "job_state": None,
        "worker_liveness": "unknown",
        "heartbeat_at": None,
        "cleanup": "unknown",
        "worktree_removed": bool(run.get("worktree_removed")),
        "cleanup_status": None,
        "cleanup_error": None,
    }
    with store.connection() as db:
        if db.execute("SELECT version FROM schema_version").fetchone()[0] >= 5:
            cleanup = db.execute(
                "SELECT status,error FROM operations WHERE run_id=? AND kind='cleanup' "
                "ORDER BY rowid DESC LIMIT 1",
                (run["id"],),
            ).fetchone()
            if cleanup:
                execution.update(cleanup_status=cleanup["status"], cleanup_error=cleanup["error"])
            row = db.execute(
                "SELECT * FROM jobs WHERE run_id=? ORDER BY rowid DESC LIMIT 1", (run["id"],)
            ).fetchone()
            if row:
                execution.update(job_state=row["state"], heartbeat_at=row["heartbeat_at"])
                if row["owner_pid"]:
                    try:
                        live = alive(
                            {"pid": row["owner_pid"], "create_time": row["owner_create_time"]}
                        )
                        execution["worker_liveness"] = (
                            "alive"
                            if live
                            else "stopped"
                            if row["state"] in {"finished", "cancelled"}
                            else "lost"
                        )
                        if (
                            live
                            and row["heartbeat_at"]
                            and time.time() - float(row["heartbeat_at"]) > 15
                        ):
                            execution["worker_liveness"] = "unresponsive"
                    except (PipelineError, ValueError):
                        pass
    if run.get("directory"):
        try:
            records = [r for _, r in markers(Path(run["directory"]))]
            execution["cleanup"] = (
                "confirmed"
                if all(
                    r.get("finished") and (r.get("protocol") != 2 or r.get("termination_confirmed"))
                    for r in records
                )
                else "pending"
            )
        except PipelineError:
            execution["cleanup"] = "manual_required"
    candidate = run.get("final_sha") or run.get("current_sha")
    validation = {
        "status": "unknown",
        "revision": run.get("revision", 0),
        "candidate_sha": candidate,
        "test_sha": (run.get("test_result") or {}).get("sha"),
        "review_sha": (run.get("review_result") or {}).get("sha"),
        "checked_at": None,
        "reason": None,
    }
    if run["state"] in {"waiting_approval", "completed"} and candidate and run.get("directory"):
        try:
            from dev_orchestra.application.handoff import verify_branch

            verify_branch(run, candidate, worktree=not run.get("worktree_removed", False))
            verified_candidate(store, run, candidate)
            if Path(run["worktree"]).exists():
                from dev_orchestra.config import Config

                git = Git(
                    Path(run["worktree"]), Config.model_validate(run["config"]).tests.output_paths
                )
                if git.sha() != candidate or git.dirty():
                    raise PipelineError("working candidate changed")
            elif (
                Git(Path(run["repo"])).call("rev-parse", "refs/heads/" + run["branch"]) != candidate
            ):
                raise PipelineError("feature branch changed")
            validation["status"] = "valid"
        except (PipelineError, OSError, KeyError):
            validation.update(
                status="invalid",
                reason="저장 증거 또는 현재 후보가 일치하지 않습니다. 상태 조정 후 확인해 주세요.",
            )
        validation["checked_at"] = now()
    elif run["state"] in {
        "queued",
        "preparing",
        "planning",
        "developing",
        "testing",
        "reviewing",
        "fixing",
        "reporting",
    }:
        validation["status"] = "pending"
    pending_control = (
        bool(run.get("control"))
        and not (run["state"] == "paused" and run["control"] == "pause")
        and not (run["state"] == "cancelled" and run["control"] == "cancel")
    )
    actions = []
    if run.get("project_id"):
        if run["state"] not in {"completed", "cancelled"} and not pending_control:
            actions.append("cancel")
        if (
            run["state"]
            in {
                "queued",
                "preparing",
                "planning",
                "developing",
                "testing",
                "reviewing",
                "fixing",
                "reporting",
            }
            and not pending_control
        ):
            actions.append("pause")
        if (
            run["state"] in {"paused", "failed", "needs_input"}
            and not execution["worktree_removed"]
        ):
            if not pending_control:
                actions.append("resume")
            if run.get("test_change_proposal") and not pending_control:
                actions.append("reject-test-changes")
        if run["state"] == "waiting_plan_approval" and not pending_control:
            actions.extend(["approve-plan", "revise-plan"])
        if run["state"] == "waiting_approval":
            actions.extend(["defer", "issue-disposition"])
            if validation["status"] == "valid":
                actions.extend(["approve", "request-changes"])
        if (
            run["state"] in {"completed", "cancelled", "failed", "needs_input"}
            and not execution["worktree_removed"]
        ):
            actions.append("cleanup")
        actions.append("reconcile")
        if run["state"] in {"completed", "cancelled"}:
            actions.append("delete")
    if run.get("deletion_pending"):
        actions = ["delete"] if run.get("project_id") else []
    return {
        "execution": execution,
        "handoff": handoff(run),
        "capacity": capacity_view(store, run),
        "workspace_progress": next(
            (
                role_progress(store, run, s)
                for s in reversed(store.steps(run["id"]))
                if s["id"] == (run.get("workspace_checkpoint") or {}).get("step_id")
            ),
            None,
        ),
        "validation": validation,
        "allowed_actions": actions,
        "deletion_pending": bool(run.get("deletion_pending")),
        "revision": run.get("revision", 0),
    }
