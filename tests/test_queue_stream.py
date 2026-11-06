"""Process-backed queue and replay tests using only disposable repositories."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from codex_stub import WORKSPACE_CLIENT
from test_workflow import legacy_service as legacy_service
from test_workflow import service as service

from dev_orchestra.application.queue import QueueService
from dev_orchestra.application.service import RunService
from dev_orchestra.infrastructure.locking import OwnershipError
from dev_orchestra.infrastructure.log_index import LogIndexer
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.infrastructure.process_owner import alive


def until(check, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("queue probe timed out")


def active_developer(svc, rid):
    root = Path(svc.store.get(rid)["directory"])
    for path in root.glob("iterations/*/developer/*/calls/*/process.process.json"):
        value = json.loads(path.read_text())
        if value.get("pid") and not value.get("finished"):
            return value
    return None


@pytest.fixture
def prepared(service, tmp_path):
    original, original_id, repo = service
    upgrade(original.store.path)
    release = tmp_path / "release"
    stub = tmp_path / "codex-stub"
    stub.write_text(f"""#!{sys.executable}
import json,sys,time
from pathlib import Path
{WORKSPACE_CLIENT}
from dev_orchestra.domain.models import AgentResult,Criterion,Coverage,ReviewResultV2
if '--help' in sys.argv:
 print('--output-schema --ephemeral --ignore-user-config --json');sys.exit(0)
req=json.loads(sys.stdin.read().split('UNTRUSTED TASK DATA:\\n',1)[1])
if req['role']=='developer' and not Path({str(release)!r}).exists():
 print('한글 진행 token=CANARY',flush=True)
 while not Path({str(release)!r}).exists():time.sleep(.05)
r=ReviewResultV2(summary='검증') if req['role']=='reviewer' and req['context'].get('review_contract')==2 else AgentResult(summary='검증')
if req['role']=='planner':r.plan='계획';r.criteria=[Criterion(id='AC1',description='조건')]
if req['role']=='tester':r.command_ids=['unit'];r.coverage=[Coverage(criterion_id='AC1',command_ids=['unit'])]
r=finish_workspace(req,r)
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(r.model_dump_json())
""")
    stub.chmod(0o755)
    config = original.config.model_copy(deep=True)
    config.codex.command = str(stub)
    config.tests.commands["unit"].argv = [
        sys.executable,
        "-c",
        "from pathlib import Path;Path('build/results.xml').write_text('<testsuite><testcase name=\"ok\"/></testsuite>')",
    ]
    svc = RunService(config)
    queue = QueueService(svc.store)
    ids = []
    dispatchers = []

    def create():
        rid = svc.create(repo, Path(original.store.get(original_id)["directory"]) / "spec.md")
        ids.append(rid)
        queue.enqueue(rid, "execute")
        return rid

    def dispatch():
        p = subprocess.Popen(
            [sys.executable, "-m", "dev_orchestra.execution.dispatcher", str(svc.store.path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        dispatchers.append(p)
        until(lambda: queue.dispatcher_status()["alive"])
        return p

    yield svc, queue, create, dispatch, release
    for rid in ids:
        run = svc.store.get(rid)
        if run["state"] not in {"completed", "cancelled"}:
            try:
                svc.cancel(rid)
            except Exception:
                pass
    for p in dispatchers:
        if p.poll() is None:
            p.terminate()
        try:
            p.wait(timeout=12)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
    for rid in ids:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with svc.store.connection() as db:
                row = db.execute(
                    "SELECT owner_pid,owner_create_time FROM jobs WHERE run_id=? AND state IN ('claimed','running')",
                    (rid,),
                ).fetchone()
            if not row or not alive({"pid": row[0], "create_time": row[1]}):
                break
            time.sleep(0.05)
        else:
            process = psutil.Process(row[0])
            if (
                process.pid != os.getpid()
                and process.create_time() == row[1]
                and "dev_orchestra.worker" in process.cmdline()
            ):
                process.kill()


def test_fifo_claim_capacity_and_stale_capability(prepared):
    svc, q, create, _, _ = prepared
    first, second = create(), create()
    job = q.reserve()
    assert job["run_id"] == first
    assert q.reserve() is None
    with pytest.raises(OwnershipError):
        q.enter(job["id"], "wrong")
    assert svc.store.get(first)["state"] == "queued"
    assert not Path(svc.store.get(second)["worktree"]).exists()


def test_expired_lease_never_steals_live_owner(prepared):
    svc, q, create, _, _ = prepared
    create()
    job = q.reserve()
    q.enter(job["id"], job["claim_token"])
    with svc.store.connection() as db:
        db.execute("UPDATE jobs SET lease_expires_at=? WHERE id=?", ("0", job["id"]))
    q.recover()
    assert q.job(job["id"])["claim_token"] == job["claim_token"]
    assert q.reserve() is None
    with svc.store.connection() as db:
        db.execute("UPDATE jobs SET state='finished' WHERE id=?", (job["id"],))


def test_dispatcher_death_worker_survives_and_finishes(prepared):
    svc, q, create, dispatch, release = prepared
    rid = create()
    d = dispatch()
    until(lambda: active_developer(svc, rid), timeout=90)
    with svc.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE run_id=?", (rid,)).fetchone())
    d.kill()
    d.wait()
    assert alive({"pid": job["owner_pid"], "create_time": job["owner_create_time"]})
    release.touch()
    until(lambda: svc.store.get(rid)["state"] == "waiting_approval", timeout=180)
    until(lambda: q.job(job["id"])["state"] == "finished")
    assert svc.events.stream_page(rid)["items"]
    svc.approve(rid)
    assert svc.store.get(rid)["state"] == "completed"


def test_worker_sigkill_recovered_without_duplicate(prepared):
    svc, q, create, dispatch, _ = prepared
    rid = create()
    dispatch()
    until(lambda: active_developer(svc, rid), timeout=90)
    with svc.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE run_id=?", (rid,)).fetchone())
    worker = psutil.Process(job["owner_pid"])
    assert worker.create_time() == job["owner_create_time"]
    worker.kill()
    until(lambda: svc.store.get(rid)["state"] in {"failed", "needs_input"})
    until(lambda: q.job(job["id"])["state"] == "lost")
    svc.cancel(rid)
    until(lambda: svc.status(rid)["state"] == "cancelled")
    assert not alive({"pid": job["owner_pid"], "create_time": job["owner_create_time"]})


def test_cli_direct_schema4_uses_queue_and_no_dispatcher(prepared):
    svc, q, create, _, release = prepared
    rid = create()
    release.touch()
    svc.start(rid)
    assert svc.status(rid)["state"] == "waiting_approval"
    with svc.store.connection() as db:
        rows = db.execute("SELECT state,owner_pid FROM jobs WHERE run_id=?", (rid,)).fetchall()
    assert len(rows) == 1 and rows[0]["state"] == "finished"
    assert not q.dispatcher_status()["alive"]


@pytest.mark.parametrize(
    "outcome,exit_code", [("waiting_approval", 0), ("failed", 2), ("needs_input", 2)]
)
def test_cli_foreground_waits_for_dispatcher_owner(prepared, monkeypatch, outcome, exit_code):
    from concurrent.futures import ThreadPoolExecutor

    from typer.testing import CliRunner

    from dev_orchestra.cli import app

    svc, q, create, dispatch, release = prepared
    stub = Path(svc.config.codex.command)
    script = stub.read_text()
    if outcome == "failed":
        script = script.replace(
            "r=ReviewResultV2", "if req['role']=='developer':sys.exit(7)\nr=ReviewResultV2"
        )
    elif outcome == "needs_input":
        script = script.replace(
            "Path(sys.argv", "if req['role']=='tester':r.questions=['추가 입력']\nPath(sys.argv"
        )
    stub.write_text(script)
    rid = create()
    dispatch()
    until(lambda: active_developer(svc, rid), timeout=90)
    # Simulate Dispatcher claiming immediately after the CLI's resume commit.
    monkeypatch.setattr(RunService, "resume", lambda *args, **kwargs: None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            CliRunner().invoke, app, ["--database", str(svc.store.path), "resume", rid]
        )
        try:
            time.sleep(0.15)
            assert not result.done()
        finally:
            release.touch()
        response = result.result(timeout=180)
    assert response.exit_code == exit_code, response.output
    assert svc.store.get(rid)["state"] == outcome
    with svc.store.connection() as db:
        jobs = db.execute("SELECT state FROM jobs WHERE run_id=?", (rid,)).fetchall()
    assert len(jobs) == 1 and jobs[0]["state"] == "finished"


def test_existing_worker_detach_returns_immediately(prepared):
    svc, _, create, dispatch, release = prepared
    rid = create()
    dispatch()
    until(lambda: active_developer(svc, rid), timeout=90)
    started = time.monotonic()
    try:
        pid = svc.start(rid, detached=True)
        assert time.monotonic() - started < 1
        assert svc.store.get(rid)["state"] == "developing"
        assert psutil.pid_exists(pid)
    finally:
        release.touch()


def test_wait_existing_interrupt_requests_pause(prepared, monkeypatch):
    svc, q, create, dispatch, _ = prepared
    rid = create()
    dispatch()
    until(lambda: active_developer(svc, rid), timeout=90)
    original = QueueService.job
    interrupted = False

    def interrupt_once(self, jid):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return original(self, jid)

    with svc.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE run_id=?", (rid,)).fetchone())
    monkeypatch.setattr(QueueService, "job", interrupt_once)
    svc.execution.wait_existing(rid, job)
    assert svc.store.get(rid)["state"] == "paused"
    assert original(q, job["id"])["state"] == "cancelled"


def test_reused_pid_is_not_adopted(prepared):
    svc, q, create, _, _ = prepared
    rid = create()
    job = q.reserve()
    svc.store.update(rid, "developing", next_role="developer")
    with svc.store.connection() as db:
        db.execute(
            "UPDATE jobs SET state='running',owner_pid=?,owner_create_time=? WHERE id=?",
            (os.getpid(), psutil.Process().create_time() - 1, job["id"]),
        )
    svc.start(rid)
    assert svc.store.get(rid)["state"] == "failed"
    assert q.job(job["id"])["state"] == "lost"
    assert svc.store.steps(rid) == []


@pytest.mark.parametrize("change", [{"state": "queued"}, {"claim_token": "another-attempt"}])
def test_existing_claim_change_does_not_report_success(prepared, monkeypatch, change):
    svc, q, create, _, _ = prepared
    rid = create()
    job = q.reserve()
    monkeypatch.setattr(QueueService, "job", lambda *args: {**job, **change})
    with pytest.raises(OwnershipError, match="attempt changed"):
        svc.start(rid)
    assert svc.store.steps(rid) == []


def test_log_replay_masks_split_secret_and_preserves_utf8(prepared):
    svc, q, create, _, _ = prepared
    rid = create()
    root = Path(svc.store.get(rid)["directory"])
    p = root / "worker.log"
    p.write_bytes("가".encode()[:2])
    index = LogIndexer(svc.store)
    index.index(rid)
    assert not svc.events.stream_page(rid)["items"][-1]["kind"] == "log.appended"
    p.write_text("가" * 2000 + " token=CANARY " + "나" * 2000 + "\n")
    index.index(rid)
    cursor = 0
    text = ""
    ids = []
    while True:
        page = svc.events.stream_page(rid, cursor)
        if not page["items"]:
            break
        for item in page["items"]:
            ids.append(item["id"])
            if item["kind"] == "log.appended":
                text += item["data"]["text"]
        cursor = page["next_cursor"]
    assert "CANARY" not in text and "[REDACTED]" in text
    assert text.count("가") == 2000 and text.count("나") == 2000
    before = svc.events.high_water()
    index.index(rid)
    assert svc.events.high_water() == before
    assert ids == sorted(set(ids))


def test_log_file_db_gap_and_rotation(prepared, monkeypatch):
    svc, _, create, _, _ = prepared
    rid = create()
    root = Path(svc.store.get(rid)["directory"])
    p = root / "worker.log"
    p.write_text("처음\n")
    index = LogIndexer(svc.store)
    original = svc.store.transaction
    from contextlib import contextmanager

    @contextmanager
    def fail():
        raise RuntimeError("DB failure")
        yield

    monkeypatch.setattr(svc.store, "transaction", fail)
    with pytest.raises(RuntimeError):
        index.index(rid)
    monkeypatch.setattr(svc.store, "transaction", original)
    index.index(rid)
    p.unlink()
    p.write_text("다음\n")
    index.index(rid)
    with svc.store.connection() as db:
        generations = db.execute(
            "SELECT generation FROM log_sources WHERE run_id=? ORDER BY generation", (rid,)
        ).fetchall()
    assert [r[0] for r in generations] == [1, 2]
    page = svc.events.stream_page(rid)
    texts = [e["data"]["text"] for e in page["items"] if e["kind"] == "log.appended"]
    assert texts == ["처음\n", "다음\n"]


def test_finished_history_skips_log_io_and_claims_before_scan(
    service, tmp_path, monkeypatch, record_property
):
    from dev_orchestra.application.execution import ExecutionService
    from dev_orchestra.execution.dispatcher import Dispatcher

    svc, rid, _ = service
    upgrade(svc.store.path)
    original = svc.store.get(rid)
    for number in range(10):
        root = tmp_path / f"history-{number}"
        root.mkdir()
        history_id = f"history-{number}"
        svc.store.create({**original, "id": history_id, "directory": str(root)})
        svc.store.update(history_id, "cancelled")
        for file in range(100):
            (root / f"{file:03}.log").write_text("완료 로그\n")
        LogIndexer(svc.store).index(history_id, closed=True)
    calls = []
    opened = []
    truncated = []
    index = LogIndexer.index
    open_path, open_fd, truncate = Path.open, os.open, os.ftruncate

    def tracked_path(path, *args, **kwargs):
        if path.suffix == ".log":
            opened.append(str(path))
        return open_path(path, *args, **kwargs)

    def tracked_fd(path, *args, **kwargs):
        if str(path).endswith(".log"):
            opened.append(str(path))
        return open_fd(path, *args, **kwargs)

    def tracked_truncate(*args):
        truncated.append(args)
        return truncate(*args)

    def tracked_index(self, *args, **kwargs):
        calls.append("index")
        return index(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracked_path)
    monkeypatch.setattr(os, "open", tracked_fd)
    monkeypatch.setattr(os, "ftruncate", tracked_truncate)
    monkeypatch.setattr(LogIndexer, "index", tracked_index)
    claim_times = []

    def launched(*args):
        calls.append("launch")
        claim_times.append(time.monotonic())

    monkeypatch.setattr(ExecutionService, "launch", launched)
    dispatcher = Dispatcher(svc.store)
    for _ in range(4):
        dispatcher.tick()
    assert not opened and not truncated
    calls.clear()
    QueueService(svc.store).enqueue(rid)
    started = time.monotonic()
    dispatcher.tick()
    elapsed = time.monotonic() - started
    assert calls[0] == "launch"
    assert calls.count("index") <= 5
    record_property("history_files", 1000)
    record_property("unchanged_log_opens", len(opened))
    record_property("unchanged_log_truncates", len(truncated))
    record_property("claim_seconds", claim_times[0] - started)
    record_property("tick_seconds", elapsed)


def test_log_file_budget_persists_cursor_and_final_tail(service):
    from dev_orchestra.infrastructure.log_index import IndexBudget

    svc, rid, _ = service
    upgrade(svc.store.path)
    root = Path(svc.store.get(rid)["directory"])
    for number in range(7):
        (root / f"{number}.log").write_text(f"한글 {number}\n끝 {number}")
    passes = 0
    while True:
        # Recreate the indexer each time: progress survives Dispatcher restarts.
        pending = LogIndexer(svc.store).index(
            rid, closed=True, budget=IndexBudget(files=2, deadline=time.monotonic() + 10)
        )
        passes += 1
        if not pending:
            break
        assert passes < 5
    assert passes == 4
    items = svc.events.stream_page(rid)["items"]
    texts = [e["data"]["text"] for e in items if e["kind"] == "log.appended"]
    assert texts == [line for n in range(7) for line in (f"한글 {n}\n", f"끝 {n}")]
    state = json.loads((root / "log-index-state.json").read_text())
    assert state["complete"] and state["closed"]


def test_worker_monitor_does_not_starve_other_sources(service):
    svc, rid, _ = service
    upgrade(svc.store.path)
    root = Path(svc.store.get(rid)["directory"])
    large = ("가" * 50000 + "\n") * 3
    (root / "a.log").write_text(large)
    (root / "b.log").write_text("다음 파일\n")
    index = LogIndexer(svc.store)
    assert index.index(rid, closed=True)
    with svc.store.connection() as db:
        other = db.execute(
            "SELECT indexed_offset FROM log_sources WHERE run_id=? AND relative_path='b.log'",
            (rid,),
        ).fetchone()
    assert other[0] == len("다음 파일\n".encode())
    while index.index(rid, closed=True):
        pass
    with svc.store.connection() as db:
        offsets = {
            r[0]: r[1]
            for r in db.execute(
                "SELECT relative_path,indexed_offset FROM log_sources WHERE run_id=?", (rid,)
            )
        }
    assert offsets["a.log"] == len(large.encode())


def test_finished_log_audit_handles_append_truncate_and_replace(service):
    svc, rid, _ = service
    upgrade(svc.store.path)
    root = Path(svc.store.get(rid)["directory"])
    source = root / "worker.log"
    source.write_text("처음 긴 로그\n")
    index = LogIndexer(svc.store)
    index.index(rid, closed=True)
    with source.open("a") as handle:
        handle.write("추가\n")
    # A bounded audit eventually revisits finalized history.
    index.index(rid, closed=True, rescan_interval=0)
    source.write_text("축소\n")
    index.index(rid, closed=True)
    replacement = root / "replacement"
    replacement.write_text("교체\n")
    replacement.replace(source)
    index.index(rid, closed=True)
    texts = [
        e["data"]["text"]
        for e in svc.events.stream_page(rid)["items"]
        if e["kind"] == "log.appended"
    ]
    assert texts == ["처음 긴 로그\n", "추가\n", "축소\n", "교체\n"]
    with svc.store.connection() as db:
        assert (
            db.execute("SELECT count(*) FROM log_sources WHERE run_id=?", (rid,)).fetchone()[0] == 3
        )


def test_concurrent_claim_has_one_winner(prepared):
    from concurrent.futures import ThreadPoolExecutor

    svc, q, create, _, _ = prepared
    create()
    create()
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: QueueService(svc.store).reserve(), range(2)))
    assert sum(value is not None for value in values) == 1


def test_claim_before_spawn_is_requeued_but_old_token_rejected(prepared):
    svc, q, create, _, _ = prepared
    rid = create()
    old = q.reserve()
    with svc.store.connection() as db:
        db.execute("UPDATE jobs SET launch_deadline=0 WHERE id=?", (old["id"],))
    q.recover()
    assert q.job(old["id"])["state"] == "queued"
    new = q.reserve()
    assert new["claim_token"] != old["claim_token"]
    with pytest.raises(OwnershipError):
        q.enter(old["id"], old["claim_token"])
    assert svc.store.get(rid)["state"] == "queued"


def test_dispatcher_restart_adopts_owner_and_resume(prepared):
    svc, q, create, dispatch, release = prepared
    rid = create()
    first = dispatch()
    until(lambda: active_developer(svc, rid), timeout=90)
    before = next(s for s in svc.store.steps(rid) if s["role"] == "developer")
    with svc.store.connection() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE run_id=?", (rid,)).fetchone())
    first.kill()
    first.wait()
    dispatch()
    time.sleep(0.5)
    assert q.job(job["id"])["owner_pid"] == job["owner_pid"]
    assert q.job(job["id"])["claim_token"] == job["claim_token"]
    svc.cancel(rid, pause=True)
    until(lambda: svc.status(rid)["state"] == "paused")
    until(lambda: q.job(job["id"])["state"] == "cancelled")
    release.touch()
    svc.resume(rid)
    until(lambda: svc.store.get(rid)["state"] == "waiting_approval", timeout=180)
    steps = svc.store.steps(rid)
    assert len([s for s in steps if s["role"] == "planner" and s["status"] == "completed"]) == 1
    developers = [s for s in steps if s["role"] == "developer"]
    assert len(developers) == 1 and developers[0]["status"] == "completed"
    assert (developers[0]["id"], developers[0]["attempt"]) == (before["id"], before["attempt"])


def test_corrupt_log_view_is_explicitly_unavailable(prepared):
    svc, _, create, _, _ = prepared
    rid = create()
    root = Path(svc.store.get(rid)["directory"])
    (root / "worker.log").write_text("내용\n")
    LogIndexer(svc.store).index(rid)
    next((root / "log-views").glob("*.log")).write_text("tampered")
    page = svc.events.stream_page(rid)
    assert any(e["kind"] == "log.unavailable" for e in page["items"])


def test_slow_sse_sender_times_out_without_worker_controls():
    import asyncio

    from dev_orchestra.api.stream import BoundedEventResponse

    async def exercise():
        async def source():
            yield b'id: 1\ndata: {"ok":true}\n\n'

        async def receive():
            await asyncio.sleep(2)
            return {"type": "http.disconnect"}

        async def send(message):
            await asyncio.sleep(2)

        response = BoundedEventResponse(source())
        response.send_timeout = 0.02
        start = time.monotonic()
        await response(
            {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET"}, receive, send
        )
        assert time.monotonic() - start < 0.5

    asyncio.run(exercise())


def test_actual_http_sse_replay_and_api_restart(prepared, tmp_path):
    import socket

    import httpx
    import yaml

    from dev_orchestra.api.security import bootstrap

    svc, q, create, dispatch, _ = prepared
    rid = create()
    root = Path(svc.store.get(rid)["directory"])
    (root / "worker.log").write_text("보존 token=CANARY\n")
    LogIndexer(svc.store).index(rid)
    settings = tmp_path / "server.yaml"
    settings.write_text(yaml.safe_dump(svc.config.model_dump(mode="json")))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = "from pathlib import Path;import sys,uvicorn;from dev_orchestra.config import load_config;from dev_orchestra.api.main import create_app;uvicorn.run(create_app(load_config(Path(sys.argv[1])),[Path(sys.argv[2])],int(sys.argv[3])),host='127.0.0.1',port=int(sys.argv[3]),proxy_headers=False,log_level='error')"

    def frame(response):
        value = {}
        for line in response.iter_lines():
            if not line and "data" in value:
                return value
            if ":" in line:
                key, text = line.split(":", 1)
                value[key] = text.lstrip()
        raise AssertionError("no SSE event received")

    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Origin": f"http://127.0.0.1:{port}"},
        timeout=5,
    ) as remote:
        last = 0
        for iteration in range(2):
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(settings),
                    str(Path(svc.store.get(rid)["repo"]).parent),
                    str(port),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:

                def ready():
                    try:
                        return remote.get("/api/health").status_code == 200
                    except httpx.TransportError:
                        return False

                until(ready)
                if iteration == 0:
                    response = remote.post(
                        "/api/session", json={"bootstrap_code": bootstrap(svc.store)}
                    )
                    assert response.status_code == 201
                    with remote.stream("GET", f"/api/runs/{rid}/events?after=0") as stream:
                        assert stream.status_code == 200
                        assert "no-transform" in stream.headers["cache-control"]
                        first = frame(stream)
                        last = int(first["id"])
                    dispatch()
                    until(lambda: active_developer(svc, rid), timeout=90)
                else:
                    assert remote.get("/api/session").status_code == 200
                    with remote.stream(
                        "GET",
                        f"/api/runs/{rid}/events?after=0",
                        headers={"Last-Event-ID": str(last)},
                    ) as stream:
                        event = frame(stream)
                        assert int(event["id"]) > last
                        assert "CANARY" not in event["data"]
                    with remote.stream(
                        "GET", f"/api/runs/{rid}/events", headers={"Last-Event-ID": "999999999"}
                    ) as stream:
                        assert stream.status_code == 409
                with svc.store.connection() as db:
                    job = dict(db.execute("SELECT * FROM jobs WHERE run_id=?", (rid,)).fetchone())
                assert alive({"pid": job["owner_pid"], "create_time": job["owner_create_time"]})
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


def test_cancel_after_claim_prevents_agent_start(prepared):
    svc, q, create, _, _ = prepared
    rid = create()
    job = q.reserve()
    other = create()
    with pytest.raises(OwnershipError):
        q.enter(job["id"], job["claim_token"], run_id=other)
    svc.cancel(rid)
    svc.execution.launch(rid, job=job)
    q.recover()
    assert svc.store.get(rid)["state"] == "cancelled"
    assert svc.store.steps(rid) == []
    assert not Path(svc.store.get(rid)["worktree"]).exists()


def test_guardian_missing_recovery_fails_closed(prepared):
    svc, q, create, _, _ = prepared
    rid = create()
    job = q.reserve()
    root = Path(svc.store.get(rid)["directory"])
    marker = root / "processes" / ("a" * 32 + ".json")
    marker.parent.mkdir()
    marker.write_text(
        json.dumps({"protocol": 2, "token": "a" * 32, "guardian": None, "finished": False})
    )
    with svc.store.connection() as db:
        db.execute("UPDATE jobs SET launch_deadline=0 WHERE id=?", (job["id"],))
    try:
        q.recover()
        assert svc.store.get(rid)["state"] == "needs_input"
        assert q.job(job["id"])["state"] == "lost"
        assert q.reserve() is None
        assert not Path(svc.store.get(rid)["worktree"]).exists()
    finally:
        marker.unlink()


def test_schema3_upgrade_preserves_queue_and_history(legacy_service):
    from dev_orchestra.infrastructure.migrations import SCHEMA

    svc, rid, _ = legacy_service
    with svc.store.connection() as db:
        db.executescript(SCHEMA)
        db.execute("UPDATE schema_version SET version=3")
        db.execute(
            "INSERT INTO jobs(id,run_id,kind,source,state,created_at) VALUES('old',?,'execute','gui','queued','old')",
            (rid,),
        )
    svc.store.update(rid, error="preserved")
    previous = svc.store.get(rid)
    events = svc.store.events(rid)
    backup = upgrade(svc.store.path)
    import sqlite3

    with sqlite3.connect(backup) as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 3
    assert svc.store.get(rid) == previous
    assert svc.store.events(rid) == events
    with svc.store.connection() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert QueueService(svc.store).reserve()["id"] == "old"


def test_worker_cli_start_duplicate_status_stop(prepared, tmp_path):
    import yaml

    svc, q, _, _, _ = prepared
    config = tmp_path / "cli.yaml"
    config.write_text(yaml.safe_dump(svc.config.model_dump(mode="json")))

    # The public console script calls app; module cli has no __main__ side effect.
    executable = Path(sys.executable).parent / "devorchestra"

    def cli(*args):
        return subprocess.run(
            [str(executable), "--config", str(config), "worker", *args],
            capture_output=True,
            text=True,
            timeout=15,
        )

    try:
        result = cli("start", "--detach")
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)["alive"]
        assert cli("start", "--detach").returncode != 0
        assert json.loads(cli("status").stdout)["alive"]
        assert cli("stop").returncode == 0
        until(lambda: not q.dispatcher_status()["alive"])
    finally:
        cli("stop")


def test_api_submit_dispatcher_and_indexed_logs(prepared):
    from fastapi.testclient import TestClient
    from test_backend_api import project

    from dev_orchestra.api.main import create_app
    from dev_orchestra.api.security import bootstrap

    svc, q, _, dispatch, release = prepared
    # Use the original repository, never the current source checkout.
    repo = Path(svc.store.list()[0]["repo"])
    before = (repo / "main.py").read_bytes()
    app = create_app(svc.config, [repo.parent.resolve()])
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50001),
        headers={"Origin": "http://127.0.0.1:8000"},
    ) as c:
        login = c.post("/api/session", json={"bootstrap_code": bootstrap(svc.store)})
        assert login.status_code == 201
        c.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        p = project(c, svc, repo)
        proposal = {
            "name": "테스트 작업",
            "project_id": p["id"],
            "spec": {"kind": "text", "text": "조건 구현"},
        }
        receipt = c.post(f"/api/projects/{p['id']}/run-previews", json=proposal)
        result = c.post(
            "/api/runs",
            json={**proposal, "preview_token": receipt.json()["preview_token"]},
            headers={"Idempotency-Key": "queue-pipeline"},
        )
        assert result.status_code == 202, result.text
        rid = result.json()["run_id"]
        try:
            dispatch()
            until(lambda: active_developer(svc, rid), timeout=90)
            # CLI and API share one source of truth; a reader never starts recovery.
            assert c.get(f"/api/runs/{rid}").json()["state"] == svc.store.get(rid)["state"]
            until(
                lambda: any(
                    e["kind"] == "log.appended" and "한글 진행" in e["data"]["text"]
                    for e in svc.events.stream_page(rid)["items"]
                )
            )
            logs = c.get(f"/api/runs/{rid}/logs").json()
            history = c.get(f"/api/runs/{rid}/event-history").json()
            assert logs["schema_version"] == 2
            assert "CANARY" not in json.dumps(logs) + json.dumps(history)
            assert "[REDACTED]" in json.dumps(logs)
            release.touch()
            until(lambda: svc.store.get(rid)["state"] == "waiting_approval", timeout=180)
            until(lambda: q.job(result.json()["job_id"])["state"] == "finished")
            assert (repo / "main.py").read_bytes() == before
        finally:
            if svc.store.get(rid)["state"] not in {"cancelled", "completed"}:
                svc.cancel(rid)


def test_monitor_drains_completed_backlog_and_bounded_pages(prepared):
    from dev_orchestra.application.monitor import WorkerMonitor

    svc, q, create, _, _ = prepared
    rid = create()
    job = q.reserve()
    root = Path(svc.store.get(rid)["directory"])
    text = ("한" * 20000 + " token=CANARY\n") * 12
    (root / "worker.log").write_text(text)
    with WorkerMonitor(q, job["id"], job["claim_token"], rid):
        pass
    cursor = 0
    replay = ""
    while True:
        page = svc.events.stream_page(rid, cursor)
        assert len(json.dumps(page["items"], ensure_ascii=False).encode()) < 66000
        replay += "".join(e["data"]["text"] for e in page["items"] if e["kind"] == "log.appended")
        if not page["has_more"]:
            break
        assert page["next_cursor"] > cursor
        cursor = page["next_cursor"]
    assert replay.count("한") == 240000
    assert "CANARY" not in replay
    with svc.store.connection() as db:
        svc.store._event(db, rid, "legacy.large", {"text": "x" * 100000})
    oversized = svc.events.stream_page(rid, page["next_cursor"])
    assert oversized["items"][0]["kind"] == "event.unavailable"
    assert oversized["next_cursor"] > page["next_cursor"]


def test_status_respects_pending_claim_over_old_launch_marker(prepared):
    svc, q, create, _, _ = prepared
    rid = create()
    root = Path(svc.store.get(rid)["directory"])
    # A resumed Run retains its previous dead Worker's ownership evidence.
    for name in ["launch.json", "worker.json"]:
        (root / name).write_text(json.dumps({"pid": 99999999, "create_time": 1, "finished": True}))
    job = q.reserve()
    assert svc.status(rid)["state"] == "queued"
    assert q.job(job["id"])["claim_token"] == job["claim_token"]
    assert q.job(job["id"])["state"] == "claimed"


def test_log_window_is_bounded_paginated_and_resumes_without_gap(prepared):
    svc, _, create, _, _ = prepared
    rid = create()
    root = Path(svc.store.get(rid)["directory"])
    path = root / "worker.log"
    path.write_text("".join(f"{i}: 한글 token=CANARY " + "가" * 1500 + "\n" for i in range(40)))
    index = LogIndexer(svc.store)
    index.index(rid)
    state = svc.store.get(rid)
    window = svc.events.log_window(rid)
    assert window["has_more"]
    assert len(json.dumps(window["items"], ensure_ascii=False).encode()) < 66000
    assert "CANARY" not in json.dumps(window)
    assert "[REDACTED]" in json.dumps(window)
    ids = [e["id"] for e in window["items"]]
    assert ids == sorted(ids)
    assert ids[-1] == window["latest_cursor"]
    cursor = window["before_cursor"]
    while window["has_more"]:
        window = svc.events.log_window(rid, cursor)
        assert window["before_cursor"] < cursor
        cursor = window["before_cursor"]
        ids = [e["id"] for e in window["items"]] + ids
    assert len(ids) == len(set(ids)) == 80
    assert svc.store.get(rid) == state
    with path.open("a") as handle:
        handle.write("새 로그\n")
    index.index(rid)
    resumed = svc.events.stream_page(rid, ids[-1])
    assert [e["data"]["text"] for e in resumed["items"]] == ["새 로그\n"]


def test_log_window_step_filter_and_integrity(prepared):
    svc, _, create, _, _ = prepared
    rid = create()
    root = Path(svc.store.get(rid)["directory"])
    step, _ = svc.store.start_step(rid, "developer", 0, "a" * 40)
    path = root / "iterations" / "0" / "developer" / "attempt-1" / "process.log"
    path.parent.mkdir(parents=True)
    path.write_text("단계 로그\n")
    (root / "worker.log").write_text("시스템 로그\n")
    LogIndexer(svc.store).index(rid)
    page = svc.events.log_window(rid, step_id=step)
    assert [e["data"]["text"] for e in page["items"]] == ["단계 로그\n"]
    assert not svc.events.log_window(rid, step_id=step + 1)["items"]
    view = root / "log-views" / (page["items"][0]["data"]["log_id"] + ".log")
    view.write_text("변조\n")
    assert svc.events.log_window(rid, step_id=step)["items"][0]["kind"] == "log.unavailable"
