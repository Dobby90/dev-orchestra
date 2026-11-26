"""Explicit release boundary, immutable legacy evidence and offline measurement."""

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_workflow import legacy_service as legacy_service
from test_workspace import edit, start_session
from test_workspace import setup as setup

from dev_orchestra.application.measurement import measurement
from dev_orchestra.application.service import RunService
from dev_orchestra.application.test_selection import selected_config, selection_for_paths
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.migrations import SCHEMA, upgrade
from dev_orchestra.infrastructure.schema4 import SCHEMA as SCHEMA4
from dev_orchestra.infrastructure.store import ReadOnlyStore, Store
from dev_orchestra.infrastructure.workspace_mcp import dispatch


@pytest.fixture
def service(legacy_service):
    return legacy_service


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
    assert result["same_scope_comparison_key"] is same["same_scope_comparison_key"] is None
    svc.store.update(rid, test_scope="affected")
    other = measurement(svc.store, rid, version_label="candidate", scenario="recovery")
    assert other["test_scope"] == "affected" and other["same_scope_comparison_key"] is None
    svc.store.update(
        rid, test_result={"test_selection": {"actual_scope": "full", "included": ["unit"]}}
    )
    expanded = measurement(svc.store, rid, version_label="candidate", scenario="recovery")
    assert expanded["validation"]["actual_scope"] == "full"
    assert expanded["same_scope_comparison_key"] is None
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
        "step.interrupted",
        "step_resumed",
        "step_completed",
    ]


def test_role_table_combines_retries_without_counting_parent_telemetry_twice(setup):
    svc, rid, _ = setup
    run = svc.store.get(rid)
    for role, seconds, status in [
        ("tester", [1, 2], "reported"),
        ("tester", [3], "partial"),
        ("reviewer", [], "unknown"),
    ]:
        step, attempt = svc.store.start_step(rid, role, 0, run["base_sha"])
        directory = Path(run["directory"]) / f"iterations/000/{role}/attempt-{attempt:03}"
        if seconds:
            write(
                directory / "telemetry.json",
                {
                    "duration_seconds": sum(seconds),
                    "usage_status": status,
                    "usage": {"input_tokens": 300, "cached_input_tokens": 200, "output_tokens": 30},
                },
            )
            for index, duration in enumerate(seconds, 1):
                write(
                    directory / f"calls/call-{index:03}/telemetry.json",
                    {"duration_seconds": duration},
                )
        svc.store.finish_step(
            step, run["base_sha"], {}, status="failed" if attempt == 1 else "completed"
        )
    script = Path(__file__).resolve().parents[1] / "scripts/measure_run.py"
    cmd = [
        sys.executable,
        script,
        "--database",
        svc.store.path,
        "--run-id",
        rid,
        "--version-label",
        "fixture",
        "--scenario",
        "recovery",
    ]
    report = json.loads(subprocess.check_output(cmd, text=True))
    tester, reviewer = report["role_summary"]
    assert tester["role"] == "tester" and tester["total_attempts"] == 2
    assert tester["wall_seconds"] == sum(
        s["wall_seconds"] for s in report["stages"] if s["role"] == "tester"
    )
    assert tester["cli_seconds"] == 6 and tester["usage_status"] == "partial"
    assert tester["uncached_input_tokens"] == 200 and tester["total_tokens"] == 660
    assert reviewer["role"] == "reviewer" and reviewer["usage_status"] == "unknown"
    assert reviewer["cli_seconds"] is None and reviewer["total_tokens"] is None
    lines = subprocess.check_output([*cmd, "--format", "table"], text=True).splitlines()
    assert len(lines) == 3
    assert lines[1].split()[3:] == ["6.00", "600", "400", "200", "60", "660", "partial"]
    assert lines[2].split()[-2:] == ["—", "unknown"]


def save_measured_tests(svc, run, step, attempt, selection, *, runtime_prefix=""):
    sha = selection["candidate_sha"]
    commands = [
        {
            "command_id": key,
            "kind": command.kind,
            "argv": command.argv,
            "runtime_argv": [*command.argv, "--basetemp=" + runtime_prefix],
            "working_directory": command.working_directory,
            "status": "passed",
            "exit_code": 0,
            "passed": True,
        }
        for key, command in selected_config(run, selection).commands.items()
    ]
    evidence = {
        "sha": sha,
        "revision": run["revision"],
        "test_selection": selection,
        "execution": {"commands": commands, "status": "passed", "passed": True},
    }
    directory = Path(run["directory"]) / "iterations" / f"{run['iteration']:03}" / "tester"
    directory /= f"attempt-{attempt:03}"
    path = directory / "result.json"
    svc.store.finish_step(step, sha, {"artifact": str(path), "hash": write(path, evidence)})
    write(
        directory / "test-invocation-fixture.json",
        {"status": "passed", "commands": [{"command_id": item["command_id"]} for item in commands]},
    )
    svc.store.update(run["id"], current_sha=sha, test_result=evidence)
    return path


@pytest.mark.parametrize("version", range(2, 9))
def test_measurement_distinguishes_selected_files_under_same_command(setup, version):
    svc, rid, repo = setup
    (repo / "tests/test_second.py").write_text("def test_second():\n    assert True\n")
    git = Git(repo)
    git.commit("비교용 테스트 파일 준비")
    sha = git.sha()
    svc.store.update(rid, base_sha=sha, test_scope="affected", test_selection_contract=version)
    run = svc.store.get(rid)
    step, attempt = svc.store.start_step(rid, "tester", 0, sha)

    def measure(files, *, reason="조건 검증", label="candidate"):
        selection = selection_for_paths(
            run, sha, ["main.py"], plan=[{"command_id": "unit", "files": files, "reason": reason}]
        )
        save_measured_tests(
            svc, run, step, attempt, selection, runtime_prefix=f"/tmp/{label}/adapter"
        )
        return measurement(
            ReadOnlyStore(svc.store.path), rid, version_label=label, scenario="normal"
        )

    first = measure(["tests/test_main.py"])
    same = measure(["tests/test_main.py"], reason="같은 대상 재검증", label="baseline")
    larger = measure(["tests/test_main.py", "tests/test_second.py"])
    key = "same_scope_comparison_key"
    assert first["conditions"] == same["conditions"] == larger["conditions"]
    assert first["validation"]["included"] == larger["validation"]["included"] == ["unit"]
    assert first[key] and first[key] == same[key]
    assert larger[key] and larger[key] != first[key]
    assert first["same_scope_comparison_version"] == 2
    assert "tests/test_main.py" not in json.dumps(first) and "/tmp/candidate" not in json.dumps(
        first
    )


@pytest.mark.parametrize(
    "problem",
    [
        "old",
        "missing_hash",
        "cwd_mismatch",
        "tampered",
        "missing_invocation",
        "extra_invocation",
        "stale",
    ],
)
def test_measurement_marks_unverifiable_workload_incomparable(setup, problem):
    svc, rid, _ = setup
    run = svc.store.get(rid)
    sha = run["base_sha"]
    selection = selection_for_paths(run, sha, [])
    step, attempt = svc.store.start_step(rid, "tester", 0, sha)
    artifact = save_measured_tests(svc, run, step, attempt, selection)

    def measure():
        return measurement(
            ReadOnlyStore(svc.store.path), rid, version_label="candidate", scenario="normal"
        )

    assert measure()["same_scope_comparison_key"]
    if problem == "old":
        svc.store.update(rid, test_selection_contract=99)
    elif problem in {"missing_hash", "cwd_mismatch"}:
        evidence = json.loads(artifact.read_text())
        if problem == "missing_hash":
            evidence["test_selection"].pop("commands_hash")
        else:
            evidence["execution"]["commands"][0]["working_directory"] = "different"
        svc.store.finish_step(
            step, sha, {"artifact": str(artifact), "hash": write(artifact, evidence)}
        )
        svc.store.update(rid, test_result=evidence)
    elif problem == "tampered":
        write(artifact, {})
    elif problem == "missing_invocation":
        artifact.with_name("test-invocation-fixture.json").unlink()
    elif problem == "extra_invocation":
        original = artifact.with_name("test-invocation-fixture.json")
        write(artifact.with_name("test-invocation-extra.json"), original.read_bytes())
    else:
        svc.store.update(rid, revision=1)
    assert measure()["same_scope_comparison_key"] is None


def test_measurement_counts_workload_across_tester_stages(setup):
    svc, rid, _ = setup
    run = svc.store.get(rid)
    sha = run["base_sha"]
    selection = selection_for_paths(run, sha, [])
    reader = ReadOnlyStore(svc.store.path)
    step, attempt = svc.store.start_step(rid, "tester", 0, sha)
    save_measured_tests(svc, run, step, attempt, selection)
    single = measurement(reader, rid, version_label="single", scenario="normal")
    step, attempt = svc.store.start_step(rid, "tester", 1, sha)
    save_measured_tests(svc, run | {"iteration": 1}, step, attempt, selection)
    repeated = measurement(reader, rid, version_label="repeated", scenario="normal")
    assert single["validation"] == repeated["validation"]
    assert repeated["same_scope_comparison_key"]
    assert single["same_scope_comparison_key"] != repeated["same_scope_comparison_key"]


def test_measurement_keeps_gradle_source_paths_and_class_filters(setup):
    svc, rid, repo = setup
    java_path = "src/test/java/example/ExampleTest.java"
    kotlin_path = "src/test/kotlin/example/ExampleTest.kt"
    for name, content in {
        "settings.gradle": "rootProject.name = 'test-workload'\n",
        "build.gradle": "plugins { id 'java' }\n",
        java_path: "package example;\nclass ExampleTest {}\n",
    }.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    git = Git(repo)
    git.commit("Gradle 비교용 테스트 준비")
    base = git.sha()
    config = svc.store.get(rid)["config"]
    config["tests"]["commands"]["unit"]["argv"] = ["gradle", "test"]
    svc.store.update(
        rid, config=config, base_sha=base, test_scope="affected", test_selection_contract=8
    )
    run = svc.store.get(rid)
    step, attempt = svc.store.start_step(rid, "tester", 0, base)

    def compare(file, sha):
        selection = selection_for_paths(
            run,
            sha,
            [file],
            plan=[{"command_id": "unit", "files": [file], "reason": "클래스 검증"}],
        )
        save_measured_tests(svc, run, step, attempt, selection)
        observed = measurement(svc.store, rid, version_label="candidate", scenario="normal")
        return selection["commands_hash"], observed["same_scope_comparison_key"]

    java_hash, java_key = compare(java_path, base)
    (repo / java_path).unlink()
    kotlin = repo / kotlin_path
    kotlin.parent.mkdir(parents=True)
    kotlin.write_text("package example\nclass ExampleTest {}\n")
    git.commit("같은 클래스의 테스트 소스 이동")
    kotlin_hash, kotlin_key = compare(kotlin_path, git.sha())
    assert java_hash == kotlin_hash
    assert java_key and kotlin_key and java_key != kotlin_key

    kotlin.write_text("package changed\nclass ExampleTest {}\n")
    git.commit("선택 클래스의 패키지 변경")
    changed_hash, changed_key = compare(kotlin_path, git.sha())
    assert changed_hash != kotlin_hash
    assert changed_key and changed_key != kotlin_key


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


@pytest.mark.parametrize("version", [2, 5])
def test_new_runs_require_explicit_schema6_activation(service, version):
    svc, rid, repo = service
    if version == 5:
        upgrade(svc.store.path)
    original = svc.store.get(rid), svc.store.events(rid)
    spec = Path(original[0]["directory"]) / "spec.md"
    directories = set(svc.config.storage.runs_directory.iterdir())
    with pytest.raises(PipelineError, match="requires schema 6"):
        RunService(svc.config).create(repo, spec)
    assert original == (svc.store.get(rid), svc.store.events(rid))
    assert directories == set(svc.config.storage.runs_directory.iterdir())
    with svc.store.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == version


@pytest.mark.parametrize("contract", [None, "patch-v1", "unknown"])
def test_old_run_is_not_executed_after_schema6_upgrade(service, contract):
    from test_workflow import FakeAgent, FakeTests

    from dev_orchestra.orchestration.runner import Runner

    svc, rid, _ = service
    svc.store.update(rid, execution_contract=contract)
    upgrade(svc.store.path, workspace=True)
    before = svc.store.get(rid)
    agent = FakeAgent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "failed" and "historical runs are read-only" in run["error"]
    assert not agent.calls and not svc.store.steps(rid)
    assert not Path(run["worktree"]).exists()
    assert run["execution_contract"] == before["execution_contract"]
    assert run["base_sha"] == before["base_sha"] and run["config"] == before["config"]


@pytest.mark.parametrize("version", [2, 5])
def test_schema_number_cannot_be_bypassed_by_contract_identifier(service, version):
    from test_workflow import FakeAgent, FakeTests

    from dev_orchestra.orchestration.runner import Runner

    svc, rid, _ = service
    if version == 5:
        upgrade(svc.store.path)
    svc.store.update(rid, execution_contract="workspace-v2")

    class Agent(FakeAgent):
        def check(self, **kwargs):
            pytest.fail("schema gate must precede agent preflight")

    agent = Agent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "failed" and "requires schema 6" in run["error"]
    assert not agent.calls and not svc.store.steps(rid)
    assert not Path(run["worktree"]).exists()
