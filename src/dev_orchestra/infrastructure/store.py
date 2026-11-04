"""SQLite checkpoints and short atomic state/event transactions."""

import json
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from dev_orchestra.domain.models import PipelineError, State
from dev_orchestra.infrastructure.locking import lock


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
            os.close(fd)
        except FileExistsError:
            pass
        self.path = path
        self._transaction = ContextVar("transaction", default=None)
        with self.connection() as db:
            exists = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if exists:
                versions = [r[0] for r in db.execute("SELECT version FROM schema_version")]
                if versions in ([3], [4], [5]):
                    return
                if versions not in ([1], [2]):
                    raise PipelineError("unsupported database schema version")
            db.executescript("""
            PRAGMA journal_mode=WAL;
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
            DELETE FROM schema_version;
            INSERT INTO schema_version VALUES(2);
            CREATE TABLE IF NOT EXISTS runs(
              id TEXT PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL,
              control TEXT, version INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS steps(
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id),
              role TEXT NOT NULL, iteration INTEGER NOT NULL, attempt INTEGER NOT NULL,
              input_sha TEXT NOT NULL, output_sha TEXT, status TEXT NOT NULL,
              data TEXT NOT NULL, UNIQUE(run_id,role,iteration,attempt));
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT REFERENCES runs(id),
              timestamp TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS approvals(
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT REFERENCES runs(id),
              sha TEXT NOT NULL, evidence TEXT NOT NULL, timestamp TEXT NOT NULL);
            COMMIT;
            """)

    @contextmanager
    def connection(self):
        existing = self._transaction.get()
        if existing is not None:
            yield existing
            return
        with lock(self.path.with_suffix(".maintenance.lock"), shared=True):
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            try:
                with db:
                    yield db
            finally:
                db.close()

    @contextmanager
    def transaction(self):
        if self._transaction.get() is not None:
            raise PipelineError("nested unit of work is not allowed")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            token = self._transaction.set(db)
            try:
                yield db
            finally:
                self._transaction.reset(token)

    def create(self, data):
        with self.connection() as db:
            db.execute(
                "INSERT INTO runs(id,state,data) VALUES(?,?,?)",
                (data["id"], "queued", json.dumps(data)),
            )

    def get(self, run_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise PipelineError(f"unknown run: {run_id}")
        return {
            **json.loads(row["data"]),
            "state": row["state"],
            "control": row["control"],
            "version": row["version"],
        }

    def list(self):
        with self.connection() as db:
            return [
                self.get(r[0])
                for r in db.execute("SELECT id FROM runs ORDER BY rowid DESC").fetchall()
            ]

    def update(self, run_id, state=None, expected_version=None, **changes):
        with self.connection() as db:
            if self._transaction.get() is None:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise PipelineError("unknown run")
            if expected_version is not None and row["version"] != expected_version:
                raise PipelineError("concurrent state update")
            data = {**json.loads(row["data"]), **changes}
            if json.loads(row["data"]).get("deletion_pending"):
                raise PipelineError("run deletion is pending; retry deletion")
            target = state or row["state"]
            State(target)
            active = {
                "queued",
                "preparing",
                "planning",
                "developing",
                "testing",
                "reviewing",
                "fixing",
                "reporting",
            }
            forward = {
                "queued": {"preparing", "planning"},
                "preparing": {"planning"},
                "planning": {"developing", "waiting_plan_approval"},
                "waiting_plan_approval": {"queued", "cancelled", "paused", "needs_input", "failed"},
                "developing": {"testing"},
                "testing": {"reviewing"},
                "reviewing": {"fixing", "reporting"},
                "fixing": {"testing"},
                "reporting": {"waiting_approval"},
                "waiting_approval": {"completed", "needs_input", "paused", "cancelled", "failed"},
                "failed": {"queued", "cancelled", "paused", "needs_input"},
                "needs_input": {"queued", "cancelled", "paused"},
                "paused": {"queued", "cancelled", "needs_input"},
                "completed": {"needs_input"},
                "cancelled": set(),
            }
            allowed = forward[row["state"]] | (
                {"failed", "needs_input", "paused", "cancelled", "queued"}
                if row["state"] in active
                else set()
            )
            if row["state"] == "queued":
                resume_target = {
                    "planner": "planning",
                    "developer": "developing",
                    "tester": "testing",
                    "reviewer": "reviewing",
                    "fixer": "fixing",
                    "done": "reporting",
                }.get(data.get("next_role"))
                if resume_target:
                    allowed.add(resume_target)
            if (
                row["state"] == "waiting_approval"
                and target == "queued"
                and changes.get("revision") == json.loads(row["data"]).get("revision", 0) + 1
                and changes.get("iteration") == json.loads(row["data"]).get("iteration", 0) + 1
                and changes.get("next_role") == "fixer"
                and "final_sha" in changes
                and changes["final_sha"] is None
                and "test_result" in changes
                and changes["test_result"] is None
                and "review_result" in changes
                and changes["review_result"] is None
            ):
                allowed.add("queued")
            if target != row["state"] and target not in allowed:
                raise PipelineError(f"invalid state transition: {row['state']} -> {target}")
            db.execute(
                "UPDATE runs SET state=?,data=?,version=version+1 WHERE id=?",
                (target, json.dumps(data), run_id),
            )
            if (
                target in {"cancelled", "paused"}
                and db.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone()
            ):
                db.execute(
                    "UPDATE jobs SET state='cancelled' WHERE run_id=? AND state='queued'", (run_id,)
                )
            if db.execute("SELECT version FROM schema_version").fetchone()[0] >= 5:
                if "revision" in changes:
                    db.execute(
                        "UPDATE runs SET revision=? WHERE id=?", (changes["revision"], run_id)
                    )
                if (
                    target == "needs_input"
                    and row["control"] in {"cancel", "pause"}
                    and changes.get("error")
                ):
                    db.execute(
                        "UPDATE operations SET status='needs_input',error=? WHERE run_id=? AND kind=? AND status='accepted'",
                        (changes["error"], run_id, row["control"]),
                    )
                if target in {"cancelled", "paused"}:
                    kind = "cancel" if target == "cancelled" else "pause"
                    db.execute(
                        "UPDATE operations SET status='applied',applied_at=? WHERE run_id=? AND kind=? AND status='accepted'",
                        (now(), run_id, kind),
                    )
            self._event(
                db,
                run_id,
                "state",
                {
                    "state": target,
                    "role": data.get("next_role"),
                    "iteration": data.get("iteration", 0),
                },
            )

    def control(self, run_id, value, expected_version=None):
        if value not in {None, "pause", "cancel"}:
            raise PipelineError("invalid control request")
        if self.get(run_id).get("deletion_pending"):
            raise PipelineError("run deletion is pending; retry deletion")
        with self.connection() as db:
            if self._transaction.get() is None:
                db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT version FROM runs WHERE id=?", (run_id,)).fetchone()
            if expected_version is not None and current["version"] != expected_version:
                raise PipelineError("concurrent state update")
            cursor = db.execute(
                "UPDATE runs SET control=?,version=version+1 WHERE id=? AND state NOT IN ('completed','cancelled')",
                (value, run_id),
            )
            if not cursor.rowcount:
                raise PipelineError("terminal run cannot accept control requests")
            self._event(db, run_id, "control_requested", {"control": value})

    def start_step(self, run_id, role, iteration, sha):
        with self.connection() as db:
            attempt = db.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 FROM steps WHERE run_id=? AND role=? AND iteration=?",
                (run_id, role, iteration),
            ).fetchone()[0]
            cursor = db.execute(
                "INSERT INTO steps(run_id,role,iteration,attempt,input_sha,status,data) VALUES(?,?,?,?,?,'running','{}')",
                (run_id, role, iteration, attempt, sha),
            )
            if db.execute("SELECT version FROM schema_version").fetchone()[0] >= 5:
                db.execute(
                    "UPDATE steps SET revision=?,started_at=? WHERE id=?",
                    (self.get(run_id).get("revision", 0), now(), cursor.lastrowid),
                )
            self._event(
                db,
                run_id,
                "step_started",
                {
                    "step_id": cursor.lastrowid,
                    "role": role,
                    "iteration": iteration,
                    "attempt": attempt,
                },
            )
            return cursor.lastrowid, attempt

    def finish_step(self, step, sha, data, status="completed"):
        with self.connection() as db:
            owner = db.execute("SELECT run_id FROM steps WHERE id=?", (step,)).fetchone()
            data = {"revision": self.get(owner["run_id"]).get("revision", 0), **data}
            db.execute(
                "UPDATE steps SET status=?,output_sha=?,data=? WHERE id=?",
                (status, sha, json.dumps(data), step),
            )
            if db.execute("SELECT version FROM schema_version").fetchone()[0] >= 5:
                db.execute("UPDATE steps SET finished_at=? WHERE id=?", (now(), step))
            row = db.execute(
                "SELECT run_id,role,iteration FROM steps WHERE id=?", (step,)
            ).fetchone()
            self._event(
                db,
                row["run_id"],
                "step_completed",
                {
                    "step_id": step,
                    "role": row["role"],
                    "iteration": row["iteration"],
                    "sha": sha,
                    "artifact": data.get("artifact"),
                },
            )

    def steps(self, run_id):
        with self.connection() as db:
            return [
                {
                    **dict(r),
                    "revision": dict(r).get("revision", json.loads(r["data"]).get("revision", 0)),
                    "data": json.loads(r["data"]),
                }
                for r in db.execute("SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,))
            ]

    def _event(self, db, run_id, kind, data):
        db.execute(
            "INSERT INTO events(run_id,timestamp,kind,data) VALUES(?,?,?,?)",
            (run_id, now(), kind, json.dumps({"schema_version": 1, **data})),
        )

    def events(self, run_id, after=0):
        with self.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after)
                )
            ]

    def approve(self, run_id, sha, evidence, expected_version=None):
        with self.connection() as db:
            if self._transaction.get() is None:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,control,version FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if expected_version is not None and row and row["version"] != expected_version:
                raise PipelineError("concurrent state update")
            if not row or row["state"] != "waiting_approval" or row["control"]:
                raise PipelineError("run is not awaiting approval")
            db.execute(
                "INSERT INTO approvals(run_id,sha,evidence,timestamp) VALUES(?,?,?,?)",
                (run_id, sha, evidence, now()),
            )
            db.execute("UPDATE runs SET state='completed',version=version+1 WHERE id=?", (run_id,))
            db.execute(
                "INSERT INTO events(run_id,timestamp,kind,data) VALUES(?,?,?,?)",
                (run_id, now(), "approved", json.dumps({"sha": sha})),
            )

    def interrupt_steps(self, run_id, reason):
        with self.connection() as db:
            if db.execute("SELECT version FROM schema_version").fetchone()[0] >= 5:
                rows = db.execute(
                    "SELECT id FROM steps WHERE run_id=? AND status='running'", (run_id,)
                ).fetchall()
                for row in rows:
                    self._event(db, run_id, "step.interrupted", {"step_id": row["id"]})
                db.execute(
                    "UPDATE steps SET finished_at=? WHERE run_id=? AND status='running'",
                    (now(), run_id),
                )
            db.execute(
                "UPDATE steps SET status='interrupted',data=? WHERE run_id=? AND status='running'",
                (json.dumps({"error": reason, "finished_at": now()}), run_id),
            )
