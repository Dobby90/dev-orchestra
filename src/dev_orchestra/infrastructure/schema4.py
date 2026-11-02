"""Phase 3 queue ownership and replay indexes."""

SCHEMA = """
ALTER TABLE jobs ADD COLUMN launch_deadline REAL;
ALTER TABLE jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN error TEXT;
CREATE TABLE dispatchers(slot INTEGER PRIMARY KEY CHECK(slot=1), pid INTEGER NOT NULL,
 create_time REAL NOT NULL, heartbeat_at REAL NOT NULL, stop_requested INTEGER NOT NULL DEFAULT 0);
CREATE TABLE log_sources(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 relative_path TEXT NOT NULL, generation INTEGER NOT NULL, identity TEXT NOT NULL,
 indexed_offset INTEGER NOT NULL DEFAULT 0, view_offset INTEGER NOT NULL DEFAULT 0,
 role TEXT, step_id INTEGER REFERENCES steps(id),
 UNIQUE(run_id,relative_path,generation));
CREATE TABLE log_spans(event_id INTEGER PRIMARY KEY REFERENCES events(id),
 log_id TEXT NOT NULL REFERENCES log_sources(id), offset INTEGER NOT NULL,
 end_offset INTEGER NOT NULL, sha256 TEXT NOT NULL, level TEXT NOT NULL,
 UNIQUE(log_id,offset), CHECK(end_offset>offset));
CREATE INDEX log_sources_run ON log_sources(run_id,id);
"""
CURRENT_SCHEMA = 4
