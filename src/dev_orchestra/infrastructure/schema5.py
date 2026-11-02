"""Explicit Phase 5 evidence, revision and operation expansion."""

CURRENT_SCHEMA = 5
SCHEMA = """
ALTER TABLE steps ADD COLUMN revision INTEGER NOT NULL DEFAULT 0;
ALTER TABLE steps ADD COLUMN started_at TEXT;
ALTER TABLE steps ADD COLUMN finished_at TEXT;
ALTER TABLE operations ADD COLUMN run_id TEXT REFERENCES runs(id);
ALTER TABLE operations ADD COLUMN kind TEXT;
ALTER TABLE operations ADD COLUMN accepted_version INTEGER;
ALTER TABLE operations ADD COLUMN applied_at TEXT;
ALTER TABLE operations ADD COLUMN error TEXT;
CREATE TABLE artifacts(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 step_id INTEGER REFERENCES steps(id), kind TEXT NOT NULL, relative_path TEXT NOT NULL,
 sha256 TEXT NOT NULL, bytes INTEGER NOT NULL, created_at TEXT NOT NULL,
 UNIQUE(run_id,relative_path));
CREATE TABLE snapshots(run_id TEXT NOT NULL REFERENCES runs(id),revision INTEGER NOT NULL,
 candidate_sha TEXT NOT NULL,artifact_id TEXT NOT NULL REFERENCES artifacts(id),
 PRIMARY KEY(run_id,revision,candidate_sha));
CREATE TABLE reports(run_id TEXT NOT NULL REFERENCES runs(id),version INTEGER NOT NULL,
 artifact_id TEXT NOT NULL REFERENCES artifacts(id),PRIMARY KEY(run_id,version));
CREATE TABLE review_issues(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
 fingerprint TEXT NOT NULL, discovered_step_id INTEGER NOT NULL REFERENCES steps(id),
 severity TEXT NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL,
 UNIQUE(run_id,fingerprint));
CREATE TABLE issue_assessments(issue_id TEXT NOT NULL REFERENCES review_issues(id),
 step_id INTEGER NOT NULL REFERENCES steps(id),status TEXT NOT NULL,
 evidence TEXT NOT NULL, candidate_sha TEXT NOT NULL,
 PRIMARY KEY(issue_id,step_id));
CREATE TABLE issue_dispositions(id INTEGER PRIMARY KEY,issue_id TEXT NOT NULL REFERENCES review_issues(id),
 step_id INTEGER NOT NULL REFERENCES steps(id),status TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX artifacts_run ON artifacts(run_id,id);
CREATE INDEX operations_run ON operations(run_id,kind,status);
"""
