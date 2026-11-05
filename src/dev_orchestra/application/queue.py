"""Durable ownership: leases observe liveness, OS locks grant execution."""

import os
import time
import uuid
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.coordination import database_root, policy, repository_root
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.infrastructure.process_owner import alive, ensure_stopped, identity
from dev_orchestra.infrastructure.store import now

ACTIVE = ("claimed", "running")


class QueueService:
    def __init__(self, store):
        self.store = store

    def enabled(self):
        with self.store.connection() as db:
            return db.execute("SELECT version FROM schema_version").fetchone()[0] in {4, 5, 6}

    def dispatcher_status(self):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM dispatchers WHERE slot=1").fetchone()
        if not row:
            return {"alive": False}
        owner = {"pid": row["pid"], "create_time": row["create_time"]}
        return {
            **owner,
            "alive": alive(owner),
            "heartbeat_at": row["heartbeat_at"],
            "stop_requested": bool(row["stop_requested"]),
        }

    def job(self, job_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise OwnershipError("job ownership unavailable")
        return dict(row)

    def reserve(self, run_id=None):
        with self.store.transaction() as db:
            active = db.execute(
                "SELECT j.run_id FROM jobs j WHERE j.state IN ('claimed','running')"
            ).fetchall()
            if run_id is None:
                candidates = db.execute(
                    "SELECT j.* FROM jobs j JOIN runs r ON r.id=j.run_id WHERE j.state='queued' AND r.state='queued' AND r.control IS NULL ORDER BY j.rowid"
                ).fetchall()
            else:
                run = self.store.get(run_id)
                row = db.execute(
                    "SELECT * FROM jobs WHERE run_id=? AND state IN ('queued','claimed','running')",
                    (run_id,),
                ).fetchone()
                if row and row["state"] != "queued":
                    return dict(row)
                if run["state"] != "queued" or run["control"]:
                    raise PipelineError("run is not ready for execution")
                if not row:
                    jid = "job-" + uuid.uuid4().hex
                    db.execute(
                        "INSERT INTO jobs(id,run_id,kind,source,state,created_at) VALUES(?,?,'execute','cli','queued',?)",
                        (jid, run_id, now()),
                    )
                    row = db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
                candidates = [row]
            if len(active) >= policy(database_root(self.store.path)):
                return None
            occupied = [repository_root(self.store.get(j["run_id"])["repo"])[1] for j in active]
            row = None
            for candidate in candidates:
                common, key = repository_root(self.store.get(candidate["run_id"])["repo"])
                if occupied.count(key) < policy(common / "devorchestra-capacity-v1"):
                    row = candidate
                    break
            if row is None:
                return None
            token = uuid.uuid4().hex
            db.execute(
                "UPDATE jobs SET state='claimed',claim_token=?,claim_epoch=claim_epoch+1,attempt=attempt+1,launch_deadline=?,owner_pid=NULL,owner_create_time=NULL WHERE id=? AND state='queued'",
                (token, time.time() + 10, row["id"]),
            )
            self.store._event(db, row["run_id"], "job.claimed", {"job_id": row["id"]})
            value = dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
            value["_launch"] = True
            return value

    def launched(self, job_id, token, owner):
        with self.store.connection() as db:
            db.execute(
                "UPDATE jobs SET owner_pid=?,owner_create_time=? WHERE id=? AND claim_token=? AND state='claimed' AND owner_pid IS NULL",
                (owner["pid"], owner["create_time"], job_id, token),
            )

    def enter(self, job_id, token, run_id=None):
        owner = identity(os.getpid())
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["claim_token"] != token or row["state"] != "claimed":
                raise OwnershipError("stale job capability")
            if run_id is not None and row["run_id"] != run_id:
                raise OwnershipError("job capability belongs to another run")
            if row["owner_pid"] is not None and (
                row["owner_pid"] != owner["pid"] or row["owner_create_time"] != owner["create_time"]
            ):
                raise OwnershipError("job owned by another worker")
            run = self.store.get(row["run_id"])
            if run["state"] != "queued" or run["control"]:
                raise OwnershipError("job was cancelled before startup")
            db.execute(
                "UPDATE jobs SET owner_pid=?,owner_create_time=?,heartbeat_at=?,lease_expires_at=? WHERE id=?",
                (
                    owner["pid"],
                    owner["create_time"],
                    str(time.time()),
                    str(time.time() + 15),
                    job_id,
                ),
            )
        return owner

    def heartbeat(self, job_id, token):
        with self.store.connection() as db:
            db.execute(
                "UPDATE jobs SET heartbeat_at=?,lease_expires_at=? WHERE id=? AND claim_token=? AND owner_pid=? AND state IN ('claimed','running')",
                (str(time.time()), str(time.time() + 15), job_id, token, os.getpid()),
            )

    def running(self, job_id, token):
        with self.store.transaction() as db:
            result = db.execute(
                "UPDATE jobs SET state='running' WHERE id=? AND claim_token=? AND owner_pid=? AND state='claimed'",
                (job_id, token, os.getpid()),
            )
            if not result.rowcount:
                raise OwnershipError("job capability lost")
            self.store._event(db, self.job(job_id)["run_id"], "job.running", {"job_id": job_id})

    def finish(self, job_id, token):
        with self.store.transaction() as db:
            job = self.job(job_id)
            if (
                job["claim_token"] != token
                or job["owner_pid"] != os.getpid()
                or job["owner_create_time"] != identity(os.getpid())["create_time"]
                or job["state"] not in ACTIVE
            ):
                return
            run = self.store.get(job["run_id"])
            state = "cancelled" if run["state"] in {"cancelled", "paused"} else "finished"
            db.execute("UPDATE jobs SET state=?,finished_at=? WHERE id=?", (state, now(), job_id))
            self.store._event(
                db, run["id"], "job.finished", {"job_id": job_id, "result_state": run["state"]}
            )

    def enqueue(self, run_id, kind="resume", connection=None):
        if connection is None:
            with self.store.transaction() as db:
                return self.enqueue(run_id, kind, db)
        db = connection
        jid = "job-" + uuid.uuid4().hex
        db.execute(
            "INSERT INTO jobs(id,run_id,kind,source,state,created_at) VALUES(?,?,?,'cli','queued',?)",
            (jid, run_id, kind, now()),
        )
        self.store._event(db, run_id, "job.queued", {"job_id": jid})
        return jid

    def recover(self):
        with self.store.connection() as db:
            jobs = [
                dict(r)
                for r in db.execute("SELECT * FROM jobs WHERE state IN ('claimed','running')")
            ]
        for job in jobs:
            owner = {"pid": job["owner_pid"], "create_time": job["owner_create_time"]}
            if alive(owner):
                continue  # An expired heartbeat never authorizes stealing a live lock.
            if not job["owner_pid"] and time.time() < (job["launch_deadline"] or 0):
                continue
            run = self.store.get(job["run_id"])
            directory = Path(run["directory"])
            try:
                with lock(directory / "execution.lock"):
                    cleanup_error = None
                    try:
                        ensure_stopped(directory)
                    except PipelineError as error:
                        cleanup_error = str(error)
                    with self.store.transaction() as db:
                        current = self.job(job["id"])
                        if (
                            current["claim_token"] != job["claim_token"]
                            or current["state"] != job["state"]
                        ):
                            continue
                        # Cleanup may take time: inspect control/state again in the same commit.
                        run = self.store.get(job["run_id"])
                        if cleanup_error:
                            if run["state"] not in {"completed", "cancelled"}:
                                self.store.update(run["id"], "needs_input", error=cleanup_error)
                            state = "lost"
                        elif run["control"] and run["state"] not in {"completed", "cancelled"}:
                            self.store.interrupt_steps(
                                run["id"], "control acknowledged after Worker exit"
                            )
                            self.store.update(
                                run["id"], "cancelled" if run["control"] == "cancel" else "paused"
                            )
                            state = "cancelled"
                        elif (
                            job["state"] == "claimed"
                            and run["state"] == "queued"
                            and job["attempt"] < 3
                        ):
                            state = "queued"
                        elif run["state"] in {
                            "waiting_approval",
                            "waiting_plan_approval",
                            "completed",
                            "cancelled",
                            "paused",
                            "needs_input",
                            "failed",
                        }:
                            state = "finished"
                        else:
                            state = "lost"
                            self.store.interrupt_steps(run["id"], "Worker ownership lost")
                            self.store.update(
                                run["id"],
                                "failed",
                                error="Worker ownership lost; resume explicitly",
                            )
                        db.execute(
                            "UPDATE jobs SET state=?,claim_token=NULL,owner_pid=NULL,owner_create_time=NULL,finished_at=? WHERE id=?",
                            (state, None if state == "queued" else now(), job["id"]),
                        )
                        self.store._event(
                            db, run["id"], "job.recovered", {"job_id": job["id"], "state": state}
                        )
                    from dev_orchestra.orchestration.runner import Runner

                    Runner(self.store, None, None).report(run["id"])
            except OwnershipError:
                continue
