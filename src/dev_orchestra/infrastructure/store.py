"""SQLite checkpoints and short atomic state/event transactions."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from dev_orchestra.domain.models import PipelineError, State


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as db:
            exists = db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
            if exists:
                versions = [r[0] for r in db.execute("SELECT version FROM schema_version")]
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
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

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
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise PipelineError("unknown run")
            if expected_version is not None and row["version"] != expected_version:
                raise PipelineError("concurrent state update")
            data = {**json.loads(row["data"]), **changes}
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
                "planning": {"developing"},
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
            if target != row["state"] and target not in allowed:
                raise PipelineError(f"invalid state transition: {row['state']} -> {target}")
            db.execute(
                "UPDATE runs SET state=?,data=?,version=version+1 WHERE id=?",
                (target, json.dumps(data), run_id),
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

    def control(self, run_id, value):
        if value not in {None, "pause", "cancel"}:
            raise PipelineError("invalid control request")
        self.get(run_id)
        with self.connection() as db:
            cursor = db.execute(
                "UPDATE runs SET control=? WHERE id=? AND state NOT IN ('completed','cancelled')",
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

    def finish_step(self, step, sha, data):
        with self.connection() as db:
            db.execute(
                "UPDATE steps SET status='completed',output_sha=?,data=? WHERE id=?",
                (sha, json.dumps(data), step),
            )
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
                {**dict(r), "data": json.loads(r["data"])}
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

    def approve(self, run_id, sha, evidence):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,control FROM runs WHERE id=?", (run_id,)).fetchone()
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
            db.execute(
                "UPDATE steps SET status='interrupted',data=? WHERE run_id=? AND status='running'",
                (json.dumps({"error": reason, "finished_at": now()}), run_id),
            )
