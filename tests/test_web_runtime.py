"""Local service ownership and bundled file boundaries, without user state."""

import json
import os
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from test_queue_stream import prepared as prepared
from test_queue_stream import until
from test_workflow import service as service

from dev_orchestra.api.main import create_app
from dev_orchestra.application.web_runtime import WebRuntime
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.infrastructure.process_owner import alive


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "gui"
    root.mkdir()
    (root / "assets").mkdir()
    (root / "index.html").write_text(
        '<!doctype html><html><body><div id="root">GUI</div><script type="module" src="/assets/app.js"></script></body></html>'
    )
    (root / "assets/app.js").write_text('console.log("GUI");')
    (root / "manifest.json").write_text(
        json.dumps(
            {
                str(p.relative_to(root)): digest(p.read_bytes())
                for p in root.rglob("*")
                if p.is_file()
            }
        )
    )
    return root


def test_static_spa_is_public_but_api_and_files_stay_protected(service, assets):
    svc, _, _ = service
    upgrade(svc.store.path)
    app = create_app(svc.config, [], static_directory=assets)
    with TestClient(app, base_url="http://127.0.0.1:8000", client=("127.0.0.1", 50002)) as client:
        assert client.get("/").status_code == 200
        assert "GUI" in client.get("/runs/run-" + "a" * 16).text
        assert client.get("/api/projects").status_code == 403
        for path in ["/%61pi/projects", "/api%2Fprojects", "/%61pi%2fprojects"]:
            assert client.get(path, headers={"Origin": "http://127.0.0.1:8000"}).status_code == 401
        assert (
            client.get("/api/missing", headers={"Origin": "http://127.0.0.1:8000"}).status_code
            == 401
        )
        assert client.get("/.env").status_code == 404
        assert client.get("/manifest.json").status_code == 404
        assert client.get("/assets/unknown.js").status_code == 404
        response = client.get("/assets/app.js")
        assert response.status_code == 200 and "immutable" in response.headers["cache-control"]
        assert "script-src 'self'" in response.headers["content-security-policy"]
        (assets / "assets/app.js").write_text("tampered")
        assert client.get("/assets/app.js").status_code == 409


def test_owned_web_stop_preserves_active_worker_and_restarts(prepared, assets):
    svc, q, create, _, release = prepared
    from test_queue_stream import active_developer

    rid = create()
    runtime = WebRuntime(svc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        state = runtime.start([Path(svc.store.get(rid)["repo"]).parent], port, True, assets)
        assert state["alive"]
        assert httpx.get(f"http://127.0.0.1:{port}/").status_code == 200
        with pytest.raises(PipelineError):
            runtime.start([], port, True, assets)
        until(lambda: active_developer(svc, rid), timeout=90)
        with svc.store.connection() as db:
            job = dict(db.execute("SELECT * FROM jobs WHERE run_id=?", (rid,)).fetchone())
        runtime.stop()
        until(lambda: not runtime.status()["alive"])
        assert alive({"pid": job["owner_pid"], "create_time": job["owner_create_time"]})
        release.touch()
        until(lambda: svc.store.get(rid)["state"] == "waiting_approval", timeout=180)
        until(lambda: q.job(job["id"])["state"] == "finished")
        until(lambda: not alive({"pid": job["owner_pid"], "create_time": job["owner_create_time"]}))
        assert svc.queries.snapshot(rid)["execution"]["worker_liveness"] == "stopped"
        assert runtime.start([], port, True, assets)["alive"]
    finally:
        runtime.stop()
        until(lambda: not runtime.status()["alive"])


def test_port_conflict_does_not_stop_existing_listener(service, assets):
    svc, _, _ = service
    upgrade(svc.store.path)
    runtime = WebRuntime(svc)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(PipelineError):
            runtime.start([], port, True, assets)
        assert listener.getsockname()[1] == port
        assert not runtime.status()["alive"]


def test_immediate_restart_with_client_keepalive_connection(service, assets):
    svc, _, _ = service
    upgrade(svc.store.path)
    runtime = WebRuntime(svc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            for _ in range(2):
                runtime.start([], port, True, assets)
                assert client.get("/").status_code == 200
                # Keep the client open so the server actively closes the connection.
                runtime.stop()
                until(lambda: not runtime.status()["alive"])
    finally:
        runtime.stop()
        until(lambda: not runtime.status()["alive"])


def test_stale_process_marker_does_not_authorize_signal(service):
    svc, _, _ = service
    runtime = WebRuntime(svc)
    runtime.path.write_text(
        json.dumps({"pid": os.getpid(), "create_time": 1, "token": "old", "port": 8000})
    )
    assert runtime.stop() == {"alive": False}
    assert not svc.store.path.with_suffix(".web-stop.json").exists()
    assert os.getpid() > 0


def test_supervisor_sigkill_stops_owned_services_and_preserves_worker(prepared, assets):
    import psutil
    from test_queue_stream import active_developer

    svc, q, create, _, release = prepared
    rid = create()
    runtime = WebRuntime(svc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    children = []
    try:
        state = runtime.start([], port, True, assets)
        supervisor = psutil.Process(state["pid"])
        assert supervisor.create_time() == state["create_time"]
        children = [(p, p.create_time()) for p in supervisor.children()]
        assert len(children) == 2
        until(lambda: active_developer(svc, rid), timeout=90)
        with svc.store.connection() as db:
            job = dict(db.execute("SELECT * FROM jobs WHERE run_id=?", (rid,)).fetchone())
        supervisor.kill()
        until(lambda: not runtime.status()["alive"])
        until(lambda: all(not alive({"pid": p.pid, "create_time": birth}) for p, birth in children))
        assert alive({"pid": job["owner_pid"], "create_time": job["owner_create_time"]})
        runtime.start([], port, True, assets)
        release.touch()
        until(lambda: svc.store.get(rid)["state"] == "waiting_approval", timeout=180)
        until(lambda: q.job(job["id"])["state"] == "finished")
        assert len([s for s in svc.store.steps(rid) if s["role"] == "developer"]) == 1
    finally:
        runtime.stop()
        until(lambda: not runtime.status()["alive"])
        # Only the children captured from our own temporary supervisor may be cleaned up.
        for child, birth in children:
            if alive({"pid": child.pid, "create_time": birth}):
                child.terminate()
                try:
                    child.wait(timeout=8)
                except psutil.TimeoutExpired:
                    child.kill()


def test_web_stop_reuses_but_does_not_stop_existing_dispatcher(prepared, assets):
    svc, q, _, dispatch, _ = prepared
    dispatcher = dispatch()
    runtime = WebRuntime(svc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        assert runtime.start([], port, True, assets)["dispatcher_reused"]
        runtime.stop()
        until(lambda: not runtime.status()["alive"])
        assert dispatcher.poll() is None and q.dispatcher_status()["pid"] == dispatcher.pid
    finally:
        runtime.stop()


def test_web_explicit_cancel_active_cancels_running_and_queued(prepared, assets):
    from test_queue_stream import active_developer

    svc, q, create, _, _ = prepared
    first, second = create(), create()
    runtime = WebRuntime(svc)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    try:
        runtime.start([], port, True, assets)
        until(lambda: active_developer(svc, first), timeout=90)
        runtime.stop(cancel_active=True)
        until(lambda: not runtime.status()["alive"])
        until(lambda: svc.status(first)["state"] == "cancelled")
        until(lambda: svc.status(second)["state"] == "cancelled")
        assert svc.store.steps(second) == []
        assert not q.dispatcher_status()["alive"]
        for rid in [first, second]:

            def finished():
                with svc.store.connection() as db:
                    return not db.execute(
                        "SELECT 1 FROM jobs WHERE run_id=? AND state IN ('queued','claimed','running')",
                        (rid,),
                    ).fetchone()

            until(finished)
        assert svc.queries.snapshot(first)["execution"]["cleanup"] == "confirmed"
    finally:
        runtime.stop(cancel_active=True)
        until(lambda: not runtime.status()["alive"])


def test_startup_sigterm_cleans_api_before_dispatcher(service, assets, tmp_path):
    import subprocess
    import sys

    import psutil

    svc, _, _ = service
    upgrade(svc.store.path)
    probe = tmp_path / "readiness-probe"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    code = f"""
import time
from pathlib import Path
from dev_orchestra.execution import web
original=web.http.client.HTTPConnection.getresponse
def delayed(self):
 Path({str(probe)!r}).touch()
 time.sleep(2)
 return original(self)
web.http.client.HTTPConnection.getresponse=delayed
web.run()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    children = []
    try:
        child.stdin.write(
            json.dumps(
                {
                    "token": "startup-test",
                    "config": svc.config.model_dump(mode="json"),
                    "roots": [],
                    "port": port,
                    "assets": str(assets),
                }
            ).encode()
        )
        child.stdin.close()
        until(probe.exists)
        children = [(p, p.create_time()) for p in psutil.Process(child.pid).children()]
        assert len(children) == 1
        child.terminate()
        assert child.wait(timeout=10) == 1
        until(lambda: all(not alive({"pid": p.pid, "create_time": birth}) for p, birth in children))
        from dev_orchestra.application.queue import QueueService

        assert not QueueService(svc.store).dispatcher_status()["alive"]
        assert svc.store.path.with_suffix(".web.log").stat().st_mode & 0o777 == 0o600
        assert svc.store.path.with_suffix(".web-config.json").stat().st_mode & 0o777 == 0o600
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        for p, birth in children:
            if alive({"pid": p.pid, "create_time": birth}):
                p.terminate()


def test_caller_interrupt_during_start_does_not_leave_supervisor(service, assets, monkeypatch):
    import subprocess

    svc, _, _ = service
    upgrade(svc.store.path)
    runtime = WebRuntime(svc)
    popen = subprocess.Popen
    children = []
    calls = 0

    def launch(*args, **kwargs):
        child = popen(*args, **kwargs)
        children.append(child)
        return child

    def status():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise KeyboardInterrupt()
        return {"alive": False}

    monkeypatch.setattr(subprocess, "Popen", launch)
    monkeypatch.setattr(runtime, "status", status)
    with pytest.raises(KeyboardInterrupt):
        runtime.start([], 8000, True, assets)
    assert len(children) == 1 and children[0].poll() is not None
