"""Release gates: authenticated evidence boundaries and destructive migration failure."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_gui_integration import completed as completed
from test_workflow import service as service

from dev_orchestra.api.main import create_app
from dev_orchestra.api.security import bootstrap
from dev_orchestra.application.results import Results
from dev_orchestra.infrastructure import migrations
from dev_orchestra.infrastructure.schema4 import SCHEMA as SCHEMA4


def test_evidence_routes_auth_scope_and_read_only(completed):
    svc, rid, repo, _ = completed
    results = Results(svc.store)
    changes = results.changes(rid)
    item = changes["files"][0]
    paths = [
        f"/api/runs/{rid}/{part}"
        for part in ["tests", "reviews", "changes", "report", "logs/download"]
    ]
    artifact = f"/api/runs/{rid}/artifacts/{item['artifact_id']}"
    diff = f"/api/runs/{rid}/diff?file_id={item['file_id']}&candidate_sha={changes['candidate_sha']}&revision=0"
    paths.extend([artifact, diff])
    origin = "http://127.0.0.1:8000"
    with TestClient(
        create_app(svc.config, [repo.parent]),
        base_url=origin,
        client=("127.0.0.1", 50003),
        headers={"Origin": origin},
    ) as client:
        for path in paths:
            assert client.get(path).status_code == 401
        response = client.post("/api/session", json={"bootstrap_code": bootstrap(svc.store)})
        assert response.status_code == 201
        before = svc.store.get(rid), svc.store.steps(rid), svc.store.events(rid)
        for path in paths:
            assert client.get(path).status_code == 200, path
        assert before == (svc.store.get(rid), svc.store.steps(rid), svc.store.events(rid))
        other = svc.create(repo, Path(svc.store.get(rid)["directory"]) / "spec.md")
        assert client.get(f"/api/runs/{other}/artifacts/{item['artifact_id']}").status_code == 404
        assert client.get(f"/api/runs/{rid}/artifacts/%2e%2e%2fconfig.json").status_code == 404
        assert client.get(diff.replace(item["file_id"], "config.json")).status_code == 404
        assert client.get(diff.replace(changes["candidate_sha"], "0" * 40)).status_code == 404
        path, _ = results.artifact(rid, item["artifact_id"])
        path.write_text("tampered")
        assert client.get(artifact).status_code == 409
        client.delete("/api/session", headers={"X-CSRF-Token": response.json()["csrf_token"]})
        assert client.get(artifact).status_code == 401


@pytest.mark.parametrize("version", [2, 3, 4])
def test_failed_migration_rolls_back_and_backup_restores(service, monkeypatch, version):
    svc, rid, _ = service
    with svc.store.connection() as db:
        if version >= 3:
            db.executescript(migrations.SCHEMA)
        if version >= 4:
            db.executescript(SCHEMA4)
        db.execute("UPDATE schema_version SET version=?", (version,))
    original = svc.store.get(rid)
    monkeypatch.setattr(migrations, "SCHEMA5", migrations.SCHEMA5 + "\nINVALID SQL INJECTION;")
    with pytest.raises(sqlite3.OperationalError):
        migrations.upgrade(svc.store.path)
    with svc.store.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == version
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='artifacts'").fetchone()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert svc.store.get(rid) == original
    backups = list(svc.store.path.parent.glob("state.db.backup-*.db"))
    assert len(backups) == 1
    restored = svc.store.path.parent / "restored.db"
    with sqlite3.connect(backups[0]) as backup, sqlite3.connect(restored) as destination:
        backup.backup(destination)
        assert destination.execute("SELECT version FROM schema_version").fetchone()[0] == version
        assert destination.execute("SELECT id FROM runs").fetchone()[0] == rid
        assert destination.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert destination.execute("PRAGMA foreign_key_check").fetchall() == []


def test_gitignored_model_test_cannot_validate_uncommitted_code(service):
    from test_workflow import FakeAgent, FakeTests

    from dev_orchestra.domain.models import AgentResult, PipelineError
    from dev_orchestra.orchestration.runner import Runner

    svc, rid, repo = service

    class IgnoredTestAgent(FakeAgent):
        def run(self, request, *args):
            if request.role == "developer":
                return AgentResult(
                    summary="ignore",
                    patch="--- /dev/null\n+++ b/.gitignore\n@@ -0,0 +1 @@\n+tests/hidden.py\n",
                )
            value = super().run(request, *args)
            if request.role == "tester":
                value.patch = "--- /dev/null\n+++ b/tests/hidden.py\n@@ -0,0 +1,2 @@\n+def test_hidden():\n+    assert True\n"
            return value

    class CountingTests(FakeTests):
        calls = 0

        def run(self, *args):
            self.calls += 1
            return super().run(*args)

    tests = CountingTests()
    Runner(svc.store, IgnoredTestAgent(), tests).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "failed"
    assert "absent from committed candidate" in run["error"]
    assert tests.calls == 0
    assert (repo / "main.py").read_text() == "developer local edits\n"
    with pytest.raises(PipelineError, match="approval requires"):
        svc.approve(rid)
