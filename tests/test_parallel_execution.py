"""Barrier-backed parallel Runs and durable capacity; no model calls."""

import json
import multiprocessing as mp
import queue
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service
from test_workspace_recovery import Agent

from dev_orchestra.application.queue import QueueService
from dev_orchestra.application.service import RunService
from dev_orchestra.domain.execution import WORKSPACE_V2
from dev_orchestra.domain.models import (
    PipelineError,
    ReviewResultV2,
    StopRequested,
    WorkspaceResult,
)
from dev_orchestra.infrastructure.coordination import (
    configure,
    configure_capacity,
    database_root,
    execution_capacity,
    policy,
    repository_root,
    slot,
)
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.infrastructure.store import Store
from dev_orchestra.orchestration.runner import Runner

CTX = mp.get_context("spawn")


class BarrierAgent(Agent):
    def __init__(self, entered, release):
        super().__init__()
        self.entered, self.release = entered, release

    def run(self, req, directory, stop):
        if req.role == "reviewer" and req.context.get("review_contract") == 2:
            return ReviewResultV2(summary="독립 리뷰 완료")
        return super().run(req, directory, stop)

    def run_workspace(self, req, directory, session, stop):
        if req.role == "developer":
            file = session.root / (req.run_id + ".py")
            if file.exists():
                assert file.read_text() == "isolated = True\n"
            else:
                session.edit_file(req.run_id + ".py", None, "isolated = True\n")
            self.entered.put(req.run_id)
            while not self.release.wait(0.03):
                if action := stop():
                    raise StopRequested(action)
            return WorkspaceResult(summary="독립 변경 완료")
        return super().run_workspace(req, directory, session, stop)


def run_worker(database, rid, entered, release):
    Runner(Store(Path(database)), BarrierAgent(entered, release), FakeTests()).execute(rid)


def until(check):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.03)
    raise AssertionError("worker did not reach expected ownership boundary")


def clone_repo(repo, path):
    subprocess.run(["git", "clone", "-q", str(repo), str(path)], check=True)
    git = Git(path)
    git.call("config", "user.name", "Test")
    git.call("config", "user.email", "test@example.invalid")
    return path


@pytest.mark.parametrize("same_repository", [False, True])
@pytest.mark.parametrize("web_schema", [False, True])
def test_runs_overlap_and_preserve_isolated_results(service, tmp_path, same_repository, web_schema):
    original, initial, repo = service
    if web_schema:
        from dev_orchestra.infrastructure.migrations import upgrade

        upgrade(original.store.path)
    svc = RunService(original.config, _execution_contract=WORKSPACE_V2)
    spec = Path(svc.store.get(initial)["directory"]) / "spec.md"
    other_repo = repo if same_repository else clone_repo(repo, tmp_path / "other")
    ids = [svc.create(repo, spec), svc.create(other_repo, spec)]
    configure_capacity(svc.store.path, total=2, repo=repo, repository=2)
    configure_capacity(svc.store.path, repo=other_repo, repository=2)
    entered, release = CTX.Queue(), CTX.Event()
    processes = []
    cancel_one = not (same_repository and web_schema)
    try:
        for rid in ids[:2]:
            p = CTX.Process(target=run_worker, args=(str(svc.store.path), rid, entered, release))
            p.start()
            processes.append(p)
        assert {entered.get(timeout=30), entered.get(timeout=30)} == set(ids[:2])
        # Both are inside Developer before either can finish, including same-common-dir runs.
        a, b = [svc.store.get(rid) for rid in ids[:2]]
        b_sha = Git(Path(b["worktree"])).sha()
        if cancel_one:
            svc.cancel(ids[0], pause=True)
            processes[0].join(30)
            assert processes[0].exitcode == 0
            assert svc.store.get(ids[0])["state"] == "paused"
            assert processes[1].is_alive()
            assert (Path(b["worktree"]) / (ids[1] + ".py")).is_file()
            assert not (Path(b["worktree"]) / (ids[0] + ".py")).exists()
            assert Git(Path(b["worktree"])).sha() == b_sha
            assert (repo / "main.py").read_text() == "developer local edits\n"
            if same_repository and not web_schema:
                before = next(
                    step for step in svc.store.steps(ids[0]) if step["role"] == "developer"
                )
                svc.resume(ids[0])
                resumed = CTX.Process(
                    target=run_worker, args=(str(svc.store.path), ids[0], entered, release)
                )
                resumed.start()
                processes.append(resumed)
                assert entered.get(timeout=60) == ids[0]
                after = next(
                    step for step in svc.store.steps(ids[0]) if step["role"] == "developer"
                )
                assert (after["id"], after["attempt"], after["input_sha"]) == (
                    before["id"],
                    before["attempt"],
                    before["input_sha"],
                )
                assert processes[1].is_alive() and Git(Path(b["worktree"])).sha() == b_sha
                svc.cancel(ids[0], pause=True)
                resumed.join(30)
                assert resumed.exitcode == 0
            # Cleanup of A cannot remove B's worktree or release B's capacity record.
            svc.cancel(ids[0])
            with pytest.raises(PipelineError, match="worktree|dirty"):
                svc.cleanup(ids[0])
        else:
            release.set()  # Both candidates must commit and validate independently.
    finally:
        release.set()
        for p in processes:
            p.join(120)
            if p.is_alive():
                p.terminate()
                p.join(10)
    assert all(p.exitcode == 0 for p in processes)
    b = svc.store.get(ids[1])
    assert b["state"] == "waiting_approval", b.get("error")
    svc.approve(ids[1])
    svc.cleanup(ids[1])
    assert Git(other_repo).call("rev-parse", "refs/heads/" + b["branch"]) == b["final_sha"]
    if cancel_one:
        assert Path(a["worktree"]).exists()  # A's unfinished changes survived B cleanup.
    else:
        a = svc.store.get(ids[0])
        assert a["state"] == "waiting_approval", a.get("error")
        assert a["final_sha"] != b["final_sha"]
        for own, other in [(a, b), (b, a)]:
            names = (
                Git(repo)
                .call("diff", "--name-only", own["base_sha"], own["final_sha"])
                .splitlines()
            )
            assert own["id"] + ".py" in names
            assert other["id"] + ".py" not in names
        svc.approve(ids[0])
        svc.cleanup(ids[0])


def test_atomic_cli_dispatcher_claims_and_repository_fairness(service, tmp_path):
    svc, initial, repo = service
    from dev_orchestra.infrastructure.migrations import upgrade

    upgrade(svc.store.path)
    spec = Path(svc.store.get(initial)["directory"]) / "spec.md"
    other = clone_repo(repo, tmp_path / "other")
    ids = [initial, svc.create(repo, spec), svc.create(other, spec)]
    q = QueueService(svc.store)
    for rid in ids:
        q.enqueue(rid)
    configure_capacity(svc.store.path, total=2)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda i: QueueService(Store(svc.store.path)).reserve(ids[0] if i % 2 else None),
                range(8),
            )
        )
    claims = {r["run_id"] for r in results if r}
    assert claims == {ids[0], ids[2]}  # Same-repo head does not starve another repository.
    assert sum(bool(r and r.get("_launch")) for r in results) == 2
    assert q.reserve(ids[1]) is None
    configure_capacity(svc.store.path, total=1)
    assert q.reserve(ids[1]) is None  # Shrink keeps existing claimed owners.
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE state='claimed'").fetchone()[0] == 2


def test_cross_database_common_slots_linked_identity_and_shrink(service, tmp_path):
    svc, rid, repo = service
    run = svc.store.get(rid)
    linked = tmp_path / "linked"
    Git(repo).create(linked, "linked", run["base_sha"])
    assert repository_root(repo)[1] == repository_root(linked)[1]
    common, _ = repository_root(repo)
    root = common / "devorchestra-capacity-v1"
    configure(root, 2)
    # Different DBs converge on this exact root, regardless of registered checkout.
    assert configure_capacity(tmp_path / "second.db", repo=linked)["repository"] == 2
    entered, release = CTX.Queue(), CTX.Event()
    dirs = [tmp_path / str(i) for i in range(3)]
    for d in dirs:
        d.mkdir()
    processes = [
        CTX.Process(
            target=capacity_worker,
            args=(
                str(tmp_path / f"database-{i}.db"),
                {"repo": str(repo if i == 0 else linked), "directory": str(d)},
                entered,
                release,
            ),
        )
        for i, d in enumerate(dirs)
    ]
    try:
        for p in processes[:2]:
            p.start()
        assert {entered.get(timeout=15), entered.get(timeout=15)} == {str(d) for d in dirs[:2]}
        configure(root, 1)
        # Nonblocking probe proves all existing slots remain owned after shrink.
        for index in range(2):
            with pytest.raises(OwnershipError), lock(root / f"slot-{index:02d}.lock"):
                pass
        processes[2].start()
        with pytest.raises(queue.Empty):
            entered.get(timeout=0.3)
        release.set()
        assert entered.get(timeout=15) == str(dirs[2])
    finally:
        release.set()
        for p in processes:
            if p.pid:
                p.join(15)
                if p.is_alive():
                    p.terminate()
                    p.join()
    assert all(p.exitcode == 0 for p in processes)


@pytest.mark.parametrize("resource", ["repository", "loopback", "npm-cache", "playwright-install"])
def test_orphan_resources_fail_closed(service, tmp_path, resource):
    svc, rid, repo = service
    directory = Path(svc.store.get(rid)["directory"])
    root = tmp_path / resource
    # An unlocked OS slot is not proof that an orphaned child has stopped.
    write(
        root / "owner-00.json",
        {
            "protocol": 1,
            "owner": {"pid": 99999999, "create_time": 1},
            "directory": str(directory),
            "directory_identity": [directory.stat().st_dev, directory.stat().st_ino],
        },
    )
    write(
        directory / "processes/orphan.json",
        {
            "protocol": 2,
            "token": "a" * 32,
            "finished": False,
            "guardian": {"pid": 99999999, "create_time": 1},
        },
    )
    with pytest.raises(PipelineError, match="guardian unavailable"):
        with slot(root, directory, limit=1):
            pytest.fail("unconfirmed resource was reused")
    assert (root / "owner-00.json").exists()


def test_legacy_exclusive_gate_and_same_run_lock(service):
    svc, rid, repo = service
    run = svc.store.get(rid)
    common, _ = repository_root(repo)
    with lock(common / "devorchestra.lock"):
        calls = 0

        def stop():
            nonlocal calls
            calls += 1
            return "pause" if calls > 2 else None

        with pytest.raises(StopRequested), execution_capacity(svc.store.path, run, stop):
            pytest.fail("new worker overlapped legacy owner")
    with lock(Path(run["directory"]) / "execution.lock"):
        with pytest.raises(OwnershipError):
            Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)


def test_policy_protocol_and_cli(service):
    from typer.testing import CliRunner

    from dev_orchestra.cli import app

    svc, _, repo = service
    result = CliRunner().invoke(
        app,
        [
            "--database",
            str(svc.store.path),
            "worker",
            "capacity",
            "--total",
            "2",
            "--repo",
            str(repo),
            "--repository",
            "2",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"total": 2, "repository": 2}
    root = database_root(svc.store.path)
    write(root / "policy.json", {"protocol": 999, "limit": 2})
    with pytest.raises(PipelineError, match="unsupported capacity"):
        policy(root)


def capacity_worker(database, run, entered, release):
    with lock(Path(run["directory"]) / "execution.lock"):
        with execution_capacity(Path(database), run, lambda: None):
            entered.put(run["directory"])
            release.wait(30)


def test_cli_full_capacity_retains_queued_job(service, monkeypatch):
    from dev_orchestra.application.execution import ExecutionService
    from dev_orchestra.infrastructure.migrations import upgrade

    svc, first, repo = service
    upgrade(svc.store.path)
    q = QueueService(svc.store)
    assert q.reserve(first)["_launch"]
    second = svc.create(repo, Path(svc.store.get(first)["directory"]) / "spec.md")
    starts = []
    execution = ExecutionService(svc.store, svc.status)
    monkeypatch.setattr(execution, "ensure_dispatcher", lambda queue: starts.append(True))
    monkeypatch.setattr(execution, "launch", lambda *args: pytest.fail("capacity bypass"))
    assert execution.start(second, detached=True) is None
    assert starts == [True]
    with svc.store.connection() as db:
        assert (
            db.execute("SELECT state FROM jobs WHERE run_id=?", (second,)).fetchone()[0] == "queued"
        )


def test_metadata_mutations_do_not_reuse_orphan_repository(service):
    from dev_orchestra.infrastructure.coordination import metadata

    svc, rid, repo = service
    directory = Path(svc.store.get(rid)["directory"])
    common, _ = repository_root(repo)
    root = common / "devorchestra-capacity-v1"
    write(
        root / "owner-00.json",
        {
            "protocol": 1,
            "owner": {"pid": 99999999, "create_time": 1},
            "directory": str(directory),
            "directory_identity": [directory.stat().st_dev, directory.stat().st_ino],
        },
    )
    write(
        directory / "processes/orphan.json",
        {
            "protocol": 2,
            "token": "a" * 32,
            "finished": False,
            "guardian": {"pid": 99999999, "create_time": 1},
        },
    )
    with pytest.raises(PipelineError, match="guardian unavailable"), metadata(repo):
        pytest.fail("metadata changed while orphan termination is unknown")
    assert (root / "owner-00.json").exists()


def loopback_worker(root, worktree, command, directory, ready, result):
    import dev_orchestra.infrastructure.coordination as coordination
    from dev_orchestra.config import Tests
    from dev_orchestra.infrastructure.process import execution_scope
    from dev_orchestra.infrastructure.test_runner import TestRunner

    coordination.host_root = lambda: Path(root)
    config = Tests.model_validate({"commands": {"port": command}})
    ready.set()
    with execution_scope(Path(directory)):
        result.put(TestRunner().run(Path(worktree), config, Path(directory))["passed"])


def test_loopback_commands_share_host_resource_across_worktrees(service, tmp_path):
    import platform
    import socket
    import sys

    if platform.system() != "Darwin":
        pytest.skip("loopback sandbox requires macOS")
    svc, rid, repo = service
    base = svc.store.get(rid)["base_sha"]
    worktrees = [tmp_path / f"port-worktree-{i}" for i in range(2)]
    directories = [tmp_path / f"port-run-{i}" for i in range(2)]
    for i, (worktree, directory) in enumerate(zip(worktrees, directories, strict=True)):
        Git(repo).create(worktree, f"port-{i}", base)
        directory.mkdir()
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    releases = [tmp_path / f"port-release-{i}" for i in range(2)]
    ready = [CTX.Event(), CTX.Event()]
    results = CTX.Queue()
    root = tmp_path / "host-resources"
    processes = []
    try:
        for i in range(2):
            script = (
                "import socket,time;from pathlib import Path;"
                f"s=socket.socket();s.bind(('127.0.0.1',{port}));s.listen();"
                "Path('build/entered').write_text('bound');"
                f"release=Path({str(releases[i])!r});"
                "\nwhile not release.exists():time.sleep(.03)\n"
                "s.close();Path('build/results.xml').write_text('<testsuite><testcase name=\"port\"/></testsuite>')"
            )
            command = {
                "argv": [sys.executable, "-c", script],
                "network": "loopback",
                "junit_report": "build/results.xml",
                "timeout_seconds": 30,
            }
            p = CTX.Process(
                target=loopback_worker,
                args=(
                    str(root),
                    str(worktrees[i]),
                    command,
                    str(directories[i]),
                    ready[i],
                    results,
                ),
            )
            p.start()
            processes.append(p)
            assert ready[i].wait(10)
            if i == 0:
                until(lambda: (worktrees[0] / "build/entered").exists())
        assert not (worktrees[1] / "build/entered").exists()
        with pytest.raises(OwnershipError), lock(root / "loopback/slot-00.lock"):
            pass
        releases[0].touch()
        until(lambda: (worktrees[1] / "build/entered").exists())
        releases[1].touch()
        assert results.get(timeout=30) and results.get(timeout=30)
    finally:
        for release in releases:
            release.touch()
        for p in processes:
            p.join(40)
            if p.is_alive():
                p.terminate()
                p.join()
    assert all(p.exitcode == 0 for p in processes)


def test_retire_only_deleted_runs_confirmed_references(service, tmp_path, monkeypatch):
    import dev_orchestra.infrastructure.coordination as coordination

    svc, rid, repo = service
    run = svc.store.get(rid)
    directory = Path(run["directory"])
    common, _ = repository_root(repo)
    host = tmp_path / "host-resources"
    host.mkdir()
    monkeypatch.setattr(coordination, "host_root", lambda: host)
    roots = [database_root(svc.store.path), common / "devorchestra-capacity-v1", host / "loopback"]
    for root in roots:
        write(
            root / "owner-00.json",
            {
                "protocol": 1,
                "owner": {"pid": 99999999, "create_time": 1},
                "directory": str(directory),
                "directory_identity": [directory.stat().st_dev, directory.stat().st_ino],
            },
        )
        write(root / "owner-01.json", {"directory": str(tmp_path / "other-run")})
    with lock(directory / "execution.lock"):
        coordination.retire_run_capacity(svc.store.path, run)
    for root in roots:
        assert not (root / "owner-00.json").exists()
        assert (root / "owner-01.json").exists()
        assert (root / "slot-00.lock").exists()


def test_replaced_run_directory_cannot_authorize_resource_cleanup(service, tmp_path):
    svc, rid, _ = service
    directory = Path(svc.store.get(rid)["directory"])
    root = tmp_path / "resource"
    write(
        root / "owner-00.json",
        {
            "protocol": 1,
            "owner": {"pid": 99999999, "create_time": 1},
            "directory": str(directory),
            "directory_identity": [0, 0],
        },
    )
    with pytest.raises(PipelineError, match="invalid resource owner"), slot(root, directory):
        pytest.fail("replaced directory was trusted")
    assert (root / "owner-00.json").exists()


def test_foreground_cli_claims_only_after_capacity_return(service, monkeypatch):
    import types

    import dev_orchestra.application.execution as execution_module
    from dev_orchestra.infrastructure.migrations import upgrade

    svc, first, repo = service
    upgrade(svc.store.path)
    q = QueueService(svc.store)
    old = q.reserve(first)
    second = svc.create(repo, Path(svc.store.get(first)["directory"]) / "spec.md")
    returned = []

    def return_capacity(seconds):
        returned.append(True)
        with svc.store.connection() as db:
            db.execute("UPDATE jobs SET state='finished' WHERE id=?", (old["id"],))

    monkeypatch.setattr(execution_module, "time", types.SimpleNamespace(sleep=return_capacity))
    execution = execution_module.ExecutionService(svc.store, svc.status)
    launches = []
    monkeypatch.setattr(execution, "launch", lambda rid, detached, job: launches.append(job) or 123)
    assert execution.start(second) == 123
    assert returned == [True]
    assert len(launches) == 1 and launches[0]["run_id"] == second
    assert launches[0]["_launch"]


def test_dispatcher_active_log_rotation(service, monkeypatch):
    from dev_orchestra.execution.dispatcher import Dispatcher
    from dev_orchestra.infrastructure.log_index import LogIndexer
    from dev_orchestra.infrastructure.migrations import upgrade

    svc, initial, repo = service
    upgrade(svc.store.path)
    q = QueueService(svc.store)
    spec = Path(svc.store.get(initial)["directory"]) / "spec.md"
    ids = [initial] + [svc.create(repo, spec) for _ in range(9)]
    configure_capacity(svc.store.path, total=16, repo=repo, repository=16)
    for rid in ids:
        q.enqueue(rid)
        assert q.reserve(rid)["_launch"]
    visited = []
    monkeypatch.setattr(LogIndexer, "index", lambda self, rid, *args, **kwargs: visited.append(rid))
    dispatcher = Dispatcher(svc.store)
    dispatcher.index_logs()
    assert len(visited) == 8
    dispatcher.index_logs()
    assert set(visited) == set(ids)


def test_queued_cli_confirms_dispatcher_start(service, monkeypatch):
    import dev_orchestra.application.execution as execution_module
    from dev_orchestra.application.execution import ExecutionService

    svc, _, _ = service
    started = []

    class Queue:
        def dispatcher_status(self):
            return {"alive": bool(started), "stop_requested": False}

    def spawn(*args, **kwargs):
        started.append(True)
        return object()

    monkeypatch.setattr(execution_module.subprocess, "Popen", spawn)
    execution = ExecutionService(svc.store, svc.status)
    execution.ensure_dispatcher(Queue())
    execution.ensure_dispatcher(Queue())
    assert started == [True]


def test_foreground_cli_adopts_dispatcher_claim_after_waiting(service, monkeypatch):
    import os
    import types

    import dev_orchestra.application.execution as execution_module
    from dev_orchestra.infrastructure.migrations import upgrade
    from dev_orchestra.infrastructure.process_owner import identity

    svc, first, repo = service
    upgrade(svc.store.path)
    q = QueueService(svc.store)
    old = q.reserve(first)
    second = svc.create(repo, Path(svc.store.get(first)["directory"]) / "spec.md")
    owner = identity(os.getpid())

    def dispatcher_claim(seconds):
        with svc.store.connection() as db:
            db.execute("UPDATE jobs SET state='finished' WHERE id=?", (old["id"],))
            db.execute(
                "UPDATE jobs SET state='running',claim_token='dispatcher',owner_pid=?,owner_create_time=? WHERE run_id=?",
                (owner["pid"], owner["create_time"], second),
            )
        svc.store.update(second, "planning")

    monkeypatch.setattr(
        execution_module,
        "time",
        types.SimpleNamespace(sleep=dispatcher_claim, monotonic=time.monotonic),
    )
    execution = execution_module.ExecutionService(svc.store, svc.status)
    waited = []
    monkeypatch.setattr(
        execution, "wait_existing", lambda rid, job: waited.append((rid, job["claim_token"]))
    )
    monkeypatch.setattr(execution, "launch", lambda *args: pytest.fail("duplicate worker"))
    assert execution.start(second) == owner["pid"]
    assert waited == [(second, "dispatcher")]


def test_nested_tool_scope_keeps_run_identity_and_retires_before_deletion(
    service, tmp_path, monkeypatch
):
    import dev_orchestra.infrastructure.coordination as coordination

    svc, rid, _ = service
    run = svc.store.get(rid)
    directory = Path(run["directory"])
    write(directory / "worker.json", {"finished": True})
    scope = directory / "iterations/000/tester/attempt-001"
    scope.mkdir(parents=True)
    host = tmp_path / "nested-host"
    host.mkdir()
    monkeypatch.setattr(coordination, "host_root", lambda: host)
    resource = host / "loopback"
    with slot(resource, scope, limit=1):
        owner = json.loads((resource / "owner-00.json").read_text())
        assert owner["directory"] == str(scope)
        assert owner["run_directory"] == str(directory)
    owner["owner"] = {"pid": 99999999, "create_time": 1}
    write(resource / "owner-00.json", owner)
    with lock(directory / "execution.lock"):
        coordination.retire_run_capacity(svc.store.path, run)
    assert not (resource / "owner-00.json").exists()


def test_abandoned_tool_scope_cannot_bypass_live_run_execution_lock(service, tmp_path):
    svc, rid, _ = service
    directory = Path(svc.store.get(rid)["directory"])
    scope = directory / "iterations/000/tester/attempt-001"
    scope.mkdir(parents=True)
    root = tmp_path / "resource"
    other = tmp_path / "other-run"
    other.mkdir()
    write(
        root / "owner-00.json",
        {
            "protocol": 1,
            "owner": {"pid": 99999999, "create_time": 1},
            "directory": str(scope),
            "directory_identity": [scope.stat().st_dev, scope.stat().st_ino],
            "run_directory": str(directory),
            "run_directory_identity": [directory.stat().st_dev, directory.stat().st_ino],
        },
    )
    with lock(directory / "execution.lock"):
        with pytest.raises(OwnershipError), slot(root, other, limit=1):
            pytest.fail("tool scope bypassed the parent Run lock")
    assert (root / "owner-00.json").exists()


def test_sparse_slots_above_reduced_limit_still_count(service, tmp_path):
    svc, rid, _ = service
    directory = Path(svc.store.get(rid)["directory"])
    root = tmp_path / "sparse-capacity"
    calls = 0

    def stop():
        nonlocal calls
        calls += 1
        return "pause" if calls > 3 else None

    with lock(root / "slot-02.lock"):
        with pytest.raises(StopRequested), slot(root, directory, stop):
            pytest.fail("occupied slot above reduced limit was ignored")
