"""Real sandbox commands with synthetic JUnit edge cases; no model calls."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from test_workflow import FakeAgent
from test_workflow import legacy_service as legacy_service
from test_workflow import service as service

from dev_orchestra.application.results import Results
from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as CommandsConfig
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.log_index import LogIndexer
from dev_orchestra.infrastructure.migrations import SCHEMA, upgrade
from dev_orchestra.infrastructure.schema4 import SCHEMA as SCHEMA4
from dev_orchestra.infrastructure.test_runner import (
    MAX_JUNIT_BYTES,
    TestRunner,
    junit_bundle,
    parse_junit,
    verify_junit_artifact,
)
from dev_orchestra.orchestration.runner import Runner

PASSING_REPORT = '<testsuite><testcase name="ok"/></testsuite>'
SKIPPED_REPORT = '<testsuite><testcase name="skip"><skipped/></testcase></testsuite>'
FAILED_REPORT = '<testsuite><testcase name="bad"><failure/></testcase></testsuite>'


def config(code, timeout=5, *, report="build/junit.xml"):
    return CommandsConfig(
        commands={
            "unit": Command(
                argv=[sys.executable, "-c", code],
                junit_report=report,
                timeout_seconds=timeout,
            )
        },
        output_paths=["build"],
    )


def report_command(reports, exit_code=0):
    lines = ["from pathlib import Path"]
    lines.extend(
        f"Path('build/junit-{i}.xml').write_text({xml!r})" for i, xml in enumerate(reports)
    )
    lines.append(f"raise SystemExit({exit_code})")
    return config("\n".join(lines), report="build/junit-*.xml")


@pytest.mark.parametrize(
    "reports,exit_code,status,counts",
    [
        ([PASSING_REPORT, SKIPPED_REPORT], 0, "passed", (2, 0, 1)),
        (["<testsuite/>", PASSING_REPORT], 0, "passed", (1, 0, 0)),
        ([SKIPPED_REPORT, SKIPPED_REPORT], 0, "parse_error", None),
        ([], 0, "parse_error", None),
        ([PASSING_REPORT, "<testsuite>"], 0, "parse_error", None),
        ([PASSING_REPORT, "<other><testcase/></other>"], 0, "parse_error", None),
        ([PASSING_REPORT, '<testsuite errors="1"/>'], 0, "parse_error", None),
        ([SKIPPED_REPORT, FAILED_REPORT], 0, "failed", (2, 1, 1)),
        ([PASSING_REPORT, SKIPPED_REPORT], 7, "failed", (2, 0, 1)),
    ],
    ids=[
        "mixed",
        "empty-suite",
        "all-skipped",
        "absent",
        "malformed",
        "wrong-root",
        "suite-error",
        "failure",
        "exit-code",
    ],
)
def test_junit_reports_are_judged_together(service, tmp_path, reports, exit_code, status, counts):
    _, _, repo = service
    value = TestRunner().run(repo, report_command(reports, exit_code), tmp_path / "attempt")
    command = value["commands"][0]
    assert value["status"] == command["status"] == status
    assert value["passed"] is command["passed"] is (status == "passed")
    assert command["exit_code"] == exit_code
    if counts is None:
        assert command["total"] is command["junit_path"] is command["junit_hash"] is None
    else:
        retained = Path(command["junit_path"])
        assert digest(retained.read_bytes()) == command["junit_hash"]
        assert junit_bundle(sorted(repo.glob("build/junit-*.xml"))) == retained.read_text()
        for evidence in (command, parse_junit(retained)):
            assert tuple(evidence[key] for key in ("total", "failed", "skipped")) == counts
        verify_junit_artifact(retained, command)
        with pytest.raises(PipelineError, match="recorded command result"):
            verify_junit_artifact(retained, command | {"passed": not command["passed"]})


def test_suite_error_cannot_borrow_failed_testcases_from_another_suite(tmp_path):
    report = tmp_path / "nested.xml"
    report.write_text(f'<testsuites>{FAILED_REPORT}<testsuite errors="1"/></testsuites>')
    with pytest.raises(PipelineError, match="suite failure counts"):
        parse_junit(report)


def test_junit_bundle_limits_total_source_bytes(tmp_path):
    first, second = tmp_path / "first.xml", tmp_path / "second.xml"
    for report in (first, second):
        report.write_text(PASSING_REPORT + " " * (MAX_JUNIT_BYTES // 2))
        assert parse_junit(report)["total"] == 1
    with pytest.raises(PipelineError, match="safety limit"):
        junit_bundle([first, second])


def test_mixed_junit_reports_survive_final_approval_and_cleanup(service):
    svc, rid, _ = service
    run = svc.store.get(rid)
    config = run["config"] | {
        "tests": report_command([PASSING_REPORT, SKIPPED_REPORT]).model_dump()
    }
    svc.store.update(rid, config=config)
    Runner(svc.store, FakeAgent(), TestRunner()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    command = run["test_result"]["execution"]["commands"][0]
    assert (command["total"], command["failed"], command["skipped"]) == (2, 0, 1)
    retained, _ = Results(svc.store).artifact(rid, command["junit_artifact_id"])
    before = retained.read_bytes()
    retained.write_text(PASSING_REPORT)
    with pytest.raises(PipelineError, match="JUnit evidence checksum mismatch"):
        svc.approve(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"

    retained.write_bytes(before)
    svc.approve(rid)
    assert svc.store.get(rid)["state"] == "completed"
    svc.cleanup(rid)
    assert not Path(run["worktree"]).exists()
    archived = sorted(Path(run["directory"]).glob("cleanup-reports-*/build/junit-*.xml"))
    assert [path.read_text() for path in archived] == [PASSING_REPORT, SKIPPED_REPORT]


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


def test_schema4_upgrade_keeps_log_cursor_and_snapshot_references(legacy_service):
    svc, rid, _ = legacy_service
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
