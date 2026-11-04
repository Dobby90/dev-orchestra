"""Durable idempotent commands; execution and approval share CLI safety services."""

import hashlib
import json
import uuid
from pathlib import Path

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.queue import QueueService
from dev_orchestra.application.results import Results
from dev_orchestra.application.reviews import Reviews
from dev_orchestra.application.test_changes import TestChangeService
from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process import sanitize
from dev_orchestra.infrastructure.process_owner import ensure_stopped
from dev_orchestra.infrastructure.store import now


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Commands:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    def get(self, op_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
        if not row:
            raise ApplicationError("OPERATION_NOT_FOUND", "operation not found")
        result = json.loads(row["result"])
        state = (
            self.store.get(row["run_id"])["state"]
            if row["run_id"]
            else result.get("status", "queued")
        )
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "kind": row["kind"] or "submit",
            "status": row["status"],
            "state": state,
            "result": result,
            "created_at": row["created_at"],
            "applied_at": row["applied_at"],
            "error": row["error"],
        }

    def execute(self, run_id, kind, expected_version, body, key=None):
        if not Results(self.store).enabled():
            raise PipelineError("run database-upgrade first")
        if kind not in {
            "cancel",
            "pause",
            "resume",
            "approve",
            "approve-plan",
            "revise-plan",
            "defer",
            "request-changes",
            "cleanup",
            "issue-disposition",
            "reconcile",
            "approve-test-changes",
            "reject-test-changes",
        }:
            raise PipelineError("unknown command")
        if kind == "cleanup" and body.get("confirm") is not True:
            raise PipelineError("explicit cleanup confirmation required")
        scope = f"command:{run_id}:{kind}"
        key_hash = hashlib.sha256((key or uuid.uuid4().hex).encode()).hexdigest()
        request_hash = hashlib.sha256(
            canonical({"version": expected_version, "body": body}).encode()
        ).hexdigest()

        def replay(db):
            row = db.execute(
                "SELECT * FROM operations WHERE scope=? AND key_hash=?", (scope, key_hash)
            ).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise ApplicationError("IDEMPOTENCY_CONFLICT", "key reused for another request")
                return dict(row)

        with self.store.connection() as db:
            old = replay(db)
        if old:
            if kind == "reconcile" and old["status"] == "accepted":
                self.finish_reconcile(old["id"], run_id, old["accepted_version"])
            if kind == "cleanup" and old["status"] == "accepted":
                self.finish_cleanup(old["id"], run_id, old["accepted_version"])
            return self.get(old["id"])
        test_decision = kind in {"approve-test-changes", "reject-test-changes"}
        if kind in {"resume", "request-changes"} or test_decision:
            QueueService(self.store).recover()
        run = self.store.get(run_id)
        if run.get("deletion_pending"):
            raise PipelineError("run deletion is pending; retry deletion")
        directory = Path(run["directory"])
        if kind in {"cancel", "pause"}:
            with self.store.transaction() as db:
                old = replay(db)
                if old:
                    return self.get(old["id"])
                self.service.request_control(run_id, kind, expected_version, db)
                op = self.record(
                    db,
                    run_id,
                    kind,
                    scope,
                    key_hash,
                    request_hash,
                    "accepted",
                    {"reason": sanitize(body.get("reason", ""))},
                )
            self.service.recovery._finish_idle_control(self.store.get(run_id))
            return self.get(op)
        if kind == "reconcile":
            with self.store.transaction() as db:
                old = replay(db)
                if old:
                    return self.get(old["id"])
                if self.store.get(run_id)["version"] != expected_version:
                    raise ApplicationError("VERSION_CONFLICT", "run version changed")
                op = self.record(db, run_id, kind, scope, key_hash, request_hash, "accepted", {})
            self.finish_reconcile(op, run_id, expected_version)
            return self.get(op)
        with lock(directory / "execution.lock"):
            run = self.store.get(run_id)
            with self.store.connection() as db:
                old = replay(db)
            if old:
                return self.get(old["id"])
            if run["version"] != expected_version:
                raise ApplicationError("VERSION_CONFLICT", "run version changed")
            ensure_stopped(directory)
            changes = {}
            approval = None
            if test_decision:
                test_changes = TestChangeService(self.store)
                proposal = test_changes.verify(
                    run,
                    body["proposal_hash"],
                    body["input_sha"],
                    approve=kind == "approve-test-changes",
                )
                changes = test_changes.decision_changes(
                    run, proposal, kind == "approve-test-changes", sanitize(body.get("reason", ""))
                )
            if kind in {"approve", "request-changes"}:
                approval = self.service.approvals.verify_locked(
                    run_id, expected_version, body.get("expected_sha")
                )
            if kind in {"approve-plan", "revise-plan"}:
                from dev_orchestra.application.plan_approval import decision_changes

                changes = decision_changes(
                    self.store,
                    run,
                    body["plan_hash"],
                    kind == "approve-plan",
                    body.get("reason", ""),
                )
            if kind == "resume":
                if run["state"] not in {"failed", "paused", "needs_input"}:
                    raise PipelineError("run is not resumable")
                changes = self.service.resume_changes(
                    run, body.get("answers_text"), body.get("new_max_fix_attempts")
                )
            if kind == "request-changes":
                instructions = body.get("instructions", "")
                if not instructions.strip() or len(instructions.encode()) > 65536:
                    raise PipelineError("change instructions required within 64 KiB")
                limit = (
                    body.get("new_max_fix_attempts")
                    or run["config"]["workflow"]["max_fix_attempts"]
                )
                if (
                    body.get("new_max_fix_attempts") is not None
                    and limit <= run["config"]["workflow"]["max_fix_attempts"]
                ):
                    raise PipelineError("new fix budget must increase")
                if run.get("iteration", 0) + 1 > limit:
                    raise PipelineError(
                        "increase max fix attempts explicitly before requesting changes"
                    )
                config = Config.model_validate(run["config"])
                config.workflow.max_fix_attempts = limit
                # Promote legacy findings to explicit host identities without claiming resolution.
                legacy = []
                if run.get("review_contract", 1) != 2:
                    for step in self.store.steps(run_id):
                        if step["role"] == "reviewer" and step["status"] == "completed":
                            legacy.append((step, Results(self.store).payload(run, step)))
                changes = {
                    "revision": run.get("revision", 0) + 1,
                    "iteration": run.get("iteration", 0) + 1,
                    "next_role": "fixer",
                    "final_sha": None,
                    "test_result": None,
                    "review_result": None,
                    "review_contract": 2,
                    "config": config.model_dump(mode="json"),
                    "directives": [*run.get("directives", []), instructions],
                    "error": None,
                    "questions": [],
                }
            if kind in {"defer", "issue-disposition"} and run["state"] != "waiting_approval":
                raise PipelineError("command requires waiting approval")
            if kind == "defer" and not body.get("reason", "").strip():
                raise PipelineError("deferral requires a reason")
            if kind == "cleanup" and run.get("cleanup_requested"):
                raise PipelineError("cleanup is already pending; inspect its operation")
            if kind == "cleanup" and run["state"] not in {
                "completed",
                "cancelled",
                "failed",
                "needs_input",
            }:
                raise PipelineError("active run cannot be cleaned up")
            with self.store.transaction() as db:
                if self.store.get(run_id)["version"] != expected_version:
                    raise ApplicationError("VERSION_CONFLICT", "run version changed")
                old = replay(db)
                if old:
                    return self.get(old["id"])
                result = {}
                status = "applied"
                if kind in {"resume", "approve-plan", "revise-plan"}:
                    result["job_id"] = self.service.commit_resume(
                        db, run, changes, expected_version
                    )
                    if kind != "resume":
                        self.store._event(
                            db,
                            run_id,
                            "plan.decided",
                            {"approved": kind == "approve-plan", "plan_hash": body["plan_hash"]},
                        )
                elif test_decision:
                    result["job_id"] = self.service.commit_resume(
                        db, run, changes, expected_version
                    )
                    self.store._event(
                        db,
                        run_id,
                        "test_changes.decided",
                        {"proposal_id": proposal["id"], "approved": kind == "approve-test-changes"},
                    )
                elif kind == "approve":
                    self.store.approve(run_id, *approval, expected_version=expected_version)
                elif kind == "request-changes":
                    if db.execute(
                        "SELECT 1 FROM jobs WHERE run_id=? AND state IN ('queued','claimed','running')",
                        (run_id,),
                    ).fetchone():
                        raise PipelineError("previous Worker is finalizing")
                    for step, payload in legacy:
                        Reviews(self.store).apply(run, step["id"], payload)
                    self.store.control(run_id, None, expected_version)
                    self.store.update(run_id, "queued", **changes)
                    db.execute(
                        "INSERT INTO run_directives VALUES(?,?,?,?,?,?)",
                        (
                            "directive-" + uuid.uuid4().hex,
                            run_id,
                            changes["revision"],
                            kind,
                            sanitize(instructions),
                            now(),
                        ),
                    )
                    result["job_id"] = QueueService(self.store).enqueue(
                        run_id, "request-changes", db
                    )
                    self.store._event(
                        db,
                        run_id,
                        "changes.requested",
                        {"revision": changes["revision"], "iteration": changes["iteration"]},
                    )
                elif kind == "defer":
                    self.store.update(
                        run_id,
                        deferred_reason=sanitize(body["reason"]),
                        expected_version=expected_version,
                    )
                    db.execute(
                        "INSERT INTO run_directives VALUES(?,?,?,?,?,?)",
                        (
                            "directive-" + uuid.uuid4().hex,
                            run_id,
                            run.get("revision", 0),
                            kind,
                            sanitize(body["reason"]),
                            now(),
                        ),
                    )
                    self.store._event(
                        db, run_id, "approval.deferred", {"reason": sanitize(body["reason"])}
                    )
                elif kind == "issue-disposition":
                    Reviews(self.store).disposition(
                        run, body["issue_id"], body["status"], body["reason"]
                    )
                    self.store.update(run_id, expected_version=expected_version)
                elif kind == "cleanup":
                    self.store.update(
                        run_id, expected_version=expected_version, cleanup_requested=True
                    )
                    status = "accepted"
                op = self.record(db, run_id, kind, scope, key_hash, request_hash, status, result)
        if kind == "cleanup":
            self.finish_cleanup(op, run_id, self.store.get(run_id)["version"])
        else:
            self.service.refresh_report(run_id)
        return self.get(op)

    def record(self, db, run_id, kind, scope, key_hash, request_hash, status, result):
        op = "op-" + uuid.uuid4().hex
        if kind == "cleanup":
            self.store.update(run_id, cleanup_operation=op)
        db.execute(
            "INSERT INTO operations(id,scope,key_hash,request_hash,status,result,created_at,run_id,kind,accepted_version,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                op,
                scope,
                key_hash,
                request_hash,
                status,
                canonical(result),
                now(),
                run_id,
                kind,
                self.store.get(run_id)["version"],
                now() if status == "applied" else None,
            ),
        )
        self.store._event(db, run_id, "operation." + status, {"operation_id": op, "kind": kind})
        return op

    def finish_cleanup(self, op, run_id, version):
        try:
            self.service.cleanup(run_id, expected_version=version)
            with self.store.transaction() as db:
                self.store.update(
                    run_id, cleanup_requested=False, worktree_removed=True, expected_version=version
                )
                db.execute(
                    "UPDATE operations SET status='applied',applied_at=? WHERE id=?", (now(), op)
                )
            self.service.refresh_report(run_id)
        except (PipelineError, OSError) as error:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE operations SET status='needs_input',error=? WHERE id=?",
                    (sanitize(str(error)), op),
                )
                if self.store.get(run_id).get("cleanup_operation") == op:
                    self.store.update(run_id, cleanup_requested=False, cleanup_operation=None)

    def finish_reconcile(self, op, run_id, version):
        try:
            if self.store.get(run_id)["version"] != version:
                raise PipelineError("run version changed before reconcile")
            self.service.status(run_id)
            status, error = "applied", None
        except (PipelineError, OSError) as failure:
            status, error = "needs_input", sanitize(str(failure))
        with self.store.connection() as db:
            db.execute(
                "UPDATE operations SET status=?,applied_at=?,error=? WHERE id=?",
                (status, now() if status == "applied" else None, error, op),
            )
