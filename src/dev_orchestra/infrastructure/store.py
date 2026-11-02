"""SQLite checkpoints and short atomic state/event transactions."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from dev_orchestra.domain.models import PipelineError


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connection() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
            INSERT OR IGNORE INTO schema_version VALUES(1);
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
            ''')

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
            db.execute("INSERT INTO runs(id,state,data) VALUES(?,?,?)", (data["id"], "queued", json.dumps(data)))

    def get(self, run_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise PipelineError(f"unknown run: {run_id}")
        return {**json.loads(row["data"]), "state": row["state"], "control": row["control"], "version": row["version"]}

    def list(self):
        with self.connection() as db:
            return [self.get(r[0]) for r in db.execute("SELECT id FROM runs ORDER BY rowid DESC").fetchall()]

    def update(self, run_id, state=None, **changes):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise PipelineError("unknown run")
            data = {**json.loads(row["data"]), **changes}
            target = state or row["state"]
            db.execute("UPDATE runs SET state=?,data=?,version=version+1 WHERE id=?", (target, json.dumps(data), run_id))
            db.execute("INSERT INTO events(run_id,timestamp,kind,data) VALUES(?,?,?,?)", (run_id, now(), "state", json.dumps({"state": target})))

    def control(self, run_id, value):
        self.get(run_id)
        with self.connection() as db:
            db.execute("UPDATE runs SET control=? WHERE id=?", (value, run_id))

    def start_step(self, run_id, role, iteration, sha):
        with self.connection() as db:
            attempt = db.execute("SELECT COALESCE(MAX(attempt),0)+1 FROM steps WHERE run_id=? AND role=? AND iteration=?", (run_id, role, iteration)).fetchone()[0]
            cursor = db.execute("INSERT INTO steps(run_id,role,iteration,attempt,input_sha,status,data) VALUES(?,?,?,?,?,'running','{}')", (run_id, role, iteration, attempt, sha))
            return cursor.lastrowid, attempt

    def finish_step(self, step, sha, data):
        with self.connection() as db:
            db.execute("UPDATE steps SET status='completed',output_sha=?,data=? WHERE id=?", (sha, json.dumps(data), step))

    def steps(self, run_id):
        with self.connection() as db:
            return [{**dict(r), "data": json.loads(r["data"])} for r in db.execute("SELECT * FROM steps WHERE run_id=? ORDER BY id", (run_id,))]

    def events(self, run_id, after=0):
        with self.connection() as db:
            return [dict(r) for r in db.execute("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id", (run_id, after))]

    def approve(self, run_id, sha, evidence):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state,control FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row or row["state"] != "waiting_approval" or row["control"]:
                raise PipelineError("run is not awaiting approval")
            db.execute("INSERT INTO approvals(run_id,sha,evidence,timestamp) VALUES(?,?,?,?)", (run_id, sha, evidence, now()))
            db.execute("UPDATE runs SET state='completed',version=version+1 WHERE id=?", (run_id,))
            db.execute("INSERT INTO events(run_id,timestamp,kind,data) VALUES(?,?,?,?)", (run_id, now(), "approved", json.dumps({"sha": sha})))
