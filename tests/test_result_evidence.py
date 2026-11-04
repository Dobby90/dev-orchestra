"""Real sandbox commands with synthetic JUnit edge cases; no model calls."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from test_workflow import service as service

from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as CommandsConfig
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.log_index import LogIndexer
from dev_orchestra.infrastructure.migrations import SCHEMA, upgrade
from dev_orchestra.infrastructure.schema4 import SCHEMA as SCHEMA4
from dev_orchestra.infrastructure.test_runner import TestRunner


def config(code, timeout=5):
    return CommandsConfig(
        commands={
            "unit": Command(
                argv=[sys.executable, "-c", code],
                junit_report="build/junit.xml",
                timeout_seconds=timeout,
            )
        },
        output_paths=["build"],
    )


def test_junit_failure_detail_copy_redaction_and_attempt_history(service, tmp_path):
    _, _, repo = service
    runner = TestRunner()
    xml = '<testsuite><testcase name="broken" classname="suite" file="tests/a.py" line="7"><failure message="token=CANARY">actual mismatch</failure></testcase></testsuite>'
    first = runner.run(
        repo,
        config(f"from pathlib import Path;Path('build/junit.xml').write_text({xml!r})"),
        tmp_path / "attempt1",
    )
    command = first["commands"][0]
    assert first["status"] == "failed" and first["passed"] is False
    assert command["failure_details"][0]["line"] == "7"
    assert "CANARY" not in json.dumps(command)
    copy = Path(command["junit_path"])
    before = copy.read_bytes()
    assert digest(before) == command["junit_hash"] and command["duration_seconds"] >= 0
    second = runner.run(
        repo,
        config(
            "from pathlib import Path;Path('build/junit.xml').write_text('<testsuite><testcase name=\"ok\"/></testsuite>')"
        ),
        tmp_path / "attempt2",
    )
    assert second["passed"] and copy.read_bytes() == before
    assert (repo / "main.py").read_text() == "developer local edits\n"


@pytest.mark.parametrize(
    "xml", ["broken xml", "<testsuite/>", "<testsuite><testcase><skipped/></testcase></testsuite>"]
)
def test_junit_missing_or_invalid_is_parse_error_not_zero_pass(service, tmp_path, xml):
    _, _, repo = service
    value = TestRunner().run(
        repo,
        config(f"from pathlib import Path;Path('build/junit.xml').write_text({xml!r})"),
        tmp_path / "attempt",
    )
    assert value["status"] == "parse_error" and value["passed"] is False
    assert value["commands"][0]["total"] is None


def test_timeout_has_duration_without_false_exit_code(service, tmp_path):
    _, _, repo = service
    value = TestRunner().run(repo, config("import time;time.sleep(4)", 1), tmp_path / "attempt")
    assert value["status"] == "timeout" and value["passed"] is False
    assert value["commands"][0]["exit_code"] is None
    assert value["duration_seconds"] >= 1


def test_missing_junit_retains_command_exit_code_and_redacted_diagnostic(service, tmp_path):
    _, _, repo = service
    value = TestRunner().run(
        repo,
        config("print('sh: playwright: command not found; token=CANARY');raise SystemExit(127)"),
        tmp_path / "attempt",
    )
    command = value["commands"][0]
    assert value["status"] == "parse_error"
    assert command["exit_code"] == 127 and command["total"] is None
    assert "no report files" in command["error"]
    assert (
        "exit code 127" in command["error"] and "playwright: command not found" in command["error"]
    )
    assert "CANARY" not in json.dumps(value)


def test_schema4_upgrade_keeps_log_cursor_and_snapshot_references(service):
    svc, rid, _ = service
    with svc.store.connection() as db:
        db.executescript(SCHEMA + SCHEMA4)
        db.execute("UPDATE schema_version SET version=4")
    root = Path(svc.store.get(rid)["directory"])
    (root / "worker.log").write_text("보존된 로그 token=CANARY\n")
    index = LogIndexer(svc.store)
    index.index(rid)
    with svc.store.connection() as db:
        eid = db.execute("SELECT id FROM events WHERE kind='log.appended'").fetchone()[0]
    value = index.replay(rid, eid)
    before = svc.store.get(rid)
    backup = upgrade(svc.store.path)
    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 4
    assert index.replay(rid, eid) == value and svc.store.get(rid) == before
    with svc.store.connection() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
