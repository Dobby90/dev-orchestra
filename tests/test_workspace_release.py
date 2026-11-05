"""Explicit release boundary, immutable legacy evidence and offline measurement."""

import json
import sqlite3
from pathlib import Path

import pytest
from test_workflow import service as service
from test_workspace import edit, start_session
from test_workspace import setup as setup

from dev_orchestra.application.measurement import measurement
from dev_orchestra.application.service import RunService
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.migrations import SCHEMA, upgrade
from dev_orchestra.infrastructure.schema4 import SCHEMA as SCHEMA4
from dev_orchestra.infrastructure.store import ReadOnlyStore, Store
from dev_orchestra.infrastructure.workspace_mcp import dispatch


@pytest.mark.parametrize("version", [2, 3, 4, 5])
def test_explicit_activation_backs_up_and_preserves_legacy(service, version):
    svc, rid, repo = service
    with svc.store.connection() as db:
        if version in {3, 4}:
            db.executescript(SCHEMA + (SCHEMA4 if version == 4 else ""))
            db.execute("UPDATE schema_version SET version=?", (version,))
    if version == 5:
        upgrade(svc.store.path)
    step, _ = svc.store.start_step(rid, "planner", 0, svc.store.get(rid)["base_sha"])
    svc.store.finish_step(step, None, {"error": "구버전 응답은 그대로 보존"}, status="failed")
    original = svc.store.get(rid), svc.store.steps(rid), svc.store.events(rid)
    backup = upgrade(svc.store.path, workspace=True)
    assert original[0] == svc.store.get(rid)
    assert original[2] == svc.store.events(rid)
    for before, after in zip(original[1], svc.store.steps(rid), strict=True):
        assert before == {key: after[key] for key in before}
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone() == (version,)
        assert (
            json.loads(db.execute("SELECT data FROM runs WHERE id=?", (rid,)).fetchone()[0])[
                "execution_contract"
            ]
            == "patch-v1"
        )
    reopened = RunService(svc.config)
    new = reopened.create(repo, Path(original[0]["directory"]) / "spec.md")
    assert reopened.store.get(new)["execution_contract"] == "workspace-v2"
    assert reopened.store.get(rid)["execution_contract"] == "patch-v1"
    with reopened.store.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 6
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert upgrade(svc.store.path) is None  # Never silently downgrade schema 6.
    assert upgrade(svc.store.path, workspace=True) is None
    assert Store(svc.store.path).get(new) == reopened.store.get(new)


@pytest.mark.parametrize(
    "boundary", ["execution", "maintenance", "web", "orphan", "unconfirmed_exit"]
)
def test_activation_refuses_ownership_conflicts_without_modification(service, boundary):
    svc, rid, _ = service
    upgrade(svc.store.path)
    original = svc.store.get(rid)
    if boundary in {"orphan", "unconfirmed_exit"}:
        write(
            Path(original["directory"]) / "processes/unfinished.json",
            {
                "finished": boundary == "unconfirmed_exit",
                "protocol": 2,
                "termination_confirmed": False,
            },
        )
        with pytest.raises(PipelineError, match="unconfirmed"):
            upgrade(svc.store.path, workspace=True)
    else:
        path = (
            Path(original["directory"]) / "execution.lock"
            if boundary == "execution"
            else svc.store.path.with_suffix(
                ".web.lock" if boundary == "web" else ".maintenance.lock"
            )
        )
        with lock(path, shared=boundary == "maintenance"):
            with pytest.raises(PipelineError):
                upgrade(svc.store.path, workspace=True)
    assert svc.store.get(rid) == original
    with svc.store.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 5


def test_measurement_preserves_unknown_and_counts_failed_calls_without_contents(setup):
    session, request = start_session(setup)
    svc, rid, _ = setup
    edit(session, "main.py", "source must not appear in metrics\n")
    result = dispatch(
        session,
        {"method": "tools/call", "params": {"name": "edit_file", "arguments": {"bad": "private"}}},
    )
    assert result["isError"]
    # Synthetic failed and continuation calls: parent aggregate must not be counted twice.
    directory = Path(svc.store.get(rid)["directory"]) / "iterations/000/developer/attempt-001"
    write(
        directory / "tool-metrics.json",
        json.loads((session.path.parent / "tool-metrics.json").read_text()),
    )
    for number in [1, 2]:
        call = directory / "calls" / f"call-{number:03}"
        write(call / "telemetry.json", {"duration_seconds": 1, "usage_status": "unknown"})
        write(call / "input-metrics.json", {"prompt_bytes": 100, "continuation": number == 2})
    write(directory / "telemetry.json", {"duration_seconds": 2, "usage_status": "unknown"})
    before = svc.store.get(rid), svc.store.steps(rid), svc.store.events(rid)
    result = measurement(svc.store, rid, version_label="candidate", scenario="recovery")
    assert result["usage"] is None and result["usage_status"] == "unknown"
    assert result["observed_role_calls"] == 2
    assert result["stages"][0]["tools"]["edit_file"]["calls"] == 1
    assert result["stages"][0]["calls"][1]["input"]["continuation"]
    assert all(
        text not in json.dumps(result) for text in ["private", "source must", str(session.root)]
    )
    assert before == (svc.store.get(rid), svc.store.steps(rid), svc.store.events(rid))
    same = measurement(svc.store, rid, version_label="baseline", scenario="recovery")
    assert result["same_scope_comparison_key"] == same["same_scope_comparison_key"]
    svc.store.update(rid, test_scope="affected")
    other = measurement(svc.store, rid, version_label="candidate", scenario="recovery")
    assert result["same_scope_comparison_key"] != other["same_scope_comparison_key"]
    svc.store.update(
        rid, test_result={"test_selection": {"actual_scope": "full", "included": ["unit"]}}
    )
    expanded = measurement(svc.store, rid, version_label="candidate", scenario="recovery")
    assert expanded["validation"]["actual_scope"] == "full"
    assert expanded["same_scope_comparison_key"] != other["same_scope_comparison_key"]
    write(
        directory / "telemetry.json",
        {
            "usage": {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 20},
            "usage_status": "partial",
        },
    )
    partial = measurement(svc.store, rid, version_label="candidate", scenario="recovery")
    assert partial["usage_status"] == "partial"
    assert partial["usage"] == {
        "input_tokens": 100,
        "cached_input_tokens": 70,
        "uncached_input_tokens": 30,
        "output_tokens": 20,
    }
    step_id = svc.store.steps(rid)[0]["id"]
    svc.store.interrupt_steps(rid, "보존 후 재개")
    svc.store.resume_step(step_id)
    svc.store.finish_step(step_id, request.input_sha, {})
    timed = measurement(svc.store, rid, version_label="candidate", scenario="recovery")
    assert timed["stages"][0]["wall_seconds"] >= 0
    assert timed["stages"][0]["latest_resume_seconds"] >= 0
    assert [event["kind"] for event in timed["stages"][0]["timeline"]] == [
        "step_started",
        "step_resumed",
        "step_completed",
    ]


def test_running_standalone_api_blocks_activation(service):
    from fastapi.testclient import TestClient

    from dev_orchestra.api.main import create_app

    svc, _, repo = service
    upgrade(svc.store.path)
    with TestClient(create_app(svc.config, [repo.parent])):
        with pytest.raises(PipelineError):
            upgrade(svc.store.path, workspace=True)
    assert upgrade(svc.store.path, workspace=True)


def test_public_cli_activation_is_explicit_and_idempotent(service):
    from typer.testing import CliRunner

    from dev_orchestra.cli import app

    svc, rid, _ = service
    cli = CliRunner()
    args = ["--database", str(svc.store.path), "database-upgrade", "--workspace"]
    result = cli.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert ".backup-" in result.output
    assert cli.invoke(app, args).exit_code == 0
    with svc.store.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 6
    assert svc.store.get(rid)["execution_contract"] == "patch-v1"


def test_failed_activation_rolls_back_schema_and_keeps_backup(service, monkeypatch):
    from dev_orchestra.infrastructure import migrations

    svc, rid, _ = service
    upgrade(svc.store.path)
    before = svc.store.get(rid)
    connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.startswith("UPDATE schema_version SET"):
                raise sqlite3.OperationalError("injected version failure")
            return super().execute(sql, *args)

    def connection(*args, **kwargs):
        return connect(*args, factory=FailingConnection, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(migrations.sqlite3, "connect", connection)
        with pytest.raises(sqlite3.OperationalError, match="injected"):
            upgrade(svc.store.path, workspace=True)
    assert svc.store.get(rid) == before
    with svc.store.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 5
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    backups = list(svc.store.path.parent.glob("state.db.backup-*.db"))
    assert len(backups) == 2
    for path in backups:
        with connect(path) as db:
            assert db.execute("SELECT version FROM schema_version").fetchone()[0] in {2, 5}
            assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_measurement_database_handle_has_no_write_authority(service, tmp_path):
    svc, rid, _ = service
    original = svc.store.get(rid)
    reader = ReadOnlyStore(svc.store.path)
    assert reader.get(rid) == original
    with reader.connection() as db:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM runs")
    assert svc.store.get(rid) == original
    absent = tmp_path / "absent.db"
    with pytest.raises(sqlite3.OperationalError):
        ReadOnlyStore(absent)
    assert not absent.exists()
