"""Explicit, offline, backed-up upgrade of the CLI database."""

import json
import os
import sqlite3
import uuid
from contextlib import ExitStack
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process_owner import alive, markers
from dev_orchestra.infrastructure.schema4 import SCHEMA as SCHEMA4
from dev_orchestra.infrastructure.schema5 import CURRENT_SCHEMA
from dev_orchestra.infrastructure.schema5 import SCHEMA as SCHEMA5

SCHEMA = """
CREATE TABLE projects (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, repository_path TEXT NOT NULL,
 git_common_directory TEXT NOT NULL, identity TEXT NOT NULL,
 default_base_branch TEXT NOT NULL, policy TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT);
CREATE UNIQUE INDEX active_project_path ON projects(repository_path) WHERE archived_at IS NULL;
CREATE TABLE specs(id TEXT PRIMARY KEY, text TEXT NOT NULL, sha256 TEXT NOT NULL,
 byte_length INTEGER NOT NULL, original_filename TEXT, created_at TEXT NOT NULL);
CREATE TABLE jobs(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 kind TEXT NOT NULL, source TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN
 ('queued','claimed','running','finished','cancelled','lost')), claim_token TEXT,
 claim_epoch INTEGER NOT NULL DEFAULT 0, owner_pid INTEGER, owner_create_time REAL,
 heartbeat_at TEXT, lease_expires_at TEXT, created_at TEXT NOT NULL, finished_at TEXT);
CREATE UNIQUE INDEX active_job_run ON jobs(run_id) WHERE state IN ('queued','claimed','running');
CREATE TABLE operations(id TEXT PRIMARY KEY, scope TEXT NOT NULL, key_hash TEXT NOT NULL,
 request_hash TEXT NOT NULL, status TEXT NOT NULL, result TEXT NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(scope,key_hash));
CREATE TABLE run_directives(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 revision INTEGER NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE web_sessions(session_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);
CREATE TABLE web_bootstrap_codes(code_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);
ALTER TABLE runs ADD COLUMN project_id TEXT REFERENCES projects(id);
ALTER TABLE runs ADD COLUMN spec_id TEXT REFERENCES specs(id);
ALTER TABLE runs ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE runs ADD COLUMN origin TEXT NOT NULL DEFAULT 'legacy';
CREATE INDEX runs_project ON runs(project_id,id);
CREATE INDEX events_run_cursor ON events(run_id,id);
CREATE INDEX jobs_queue ON jobs(state,created_at,id);
"""


def upgrade(path: Path):
    """Never signals a process or resets a checkout while migrating."""
    with lock(path.with_suffix(".maintenance.lock")), ExitStack() as locks:
        db = sqlite3.connect(path, timeout=10)
        try:
            version = db.execute("SELECT version FROM schema_version").fetchall()
            if version == [(CURRENT_SCHEMA,)]:
                return None
            if version not in ([(2,)], [(3,)], [(4,)]):
                raise PipelineError("upgrade requires initialized schema 2, 3 or 4")
            for (raw,) in db.execute("SELECT data FROM runs"):
                directory = Path(json.loads(raw).get("directory", path.parent / "missing"))
                locks.enter_context(lock(directory / "execution.lock"))
                for name in ["worker.json", "launch.json"]:
                    record = directory / name
                    if record.exists():
                        value = json.loads(record.read_text())
                        if not value.get("finished") and alive(value):
                            raise PipelineError("stop Workers before database upgrade")
                if any(not r.get("finished") for _, r in markers(directory)):
                    raise PipelineError("unconfirmed processes prevent database upgrade")
            backup = path.with_name(path.name + ".backup-" + uuid.uuid4().hex + ".db")
            fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            with sqlite3.connect(backup) as target:
                db.backup(target)
            counts = {
                table: db.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in ["runs", "steps", "events", "approvals"]
            }
            references = [json.loads(row[0]) for row in db.execute("SELECT data FROM steps")]
            write(
                backup.with_suffix(".manifest.json"),
                {
                    "source_schema": version[0][0],
                    "backup": backup.name,
                    "counts": counts,
                    "step_evidence_references": references,
                },
            )
            os.chmod(path, 0o600)
            for suffix in ["-wal", "-shm"]:
                sidecar = path.with_name(path.name + suffix)
                if sidecar.exists() and not sidecar.is_symlink():
                    os.chmod(sidecar, 0o600)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            for statement in (
                (SCHEMA if version == [(2,)] else "")
                + (SCHEMA4 if version[0][0] < 4 else "")
                + SCHEMA5
            ).split(";"):
                if statement.strip():
                    db.execute(statement)
            # Schema 2 stores plan revisions inside JSON checkpoints.
            db.execute("UPDATE runs SET revision=COALESCE(json_extract(data, '$.revision'), 0)")
            db.execute(
                "UPDATE steps SET revision=COALESCE(json_extract(data, '$.revision'), revision)"
            )
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise PipelineError("foreign key check failed")
            db.execute("UPDATE schema_version SET version=?", (CURRENT_SCHEMA,))
            db.commit()
            return backup
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
