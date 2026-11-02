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
        until(lambda: active_developer(svc, rid))
        with svc.store.connection() as db:
            job = dict(db.execute("SELECT * FROM jobs WHERE run_id=?", (rid,)).fetchone())
        runtime.stop()
        until(lambda: not runtime.status()["alive"])
        assert alive({"pid": job["owner_pid"], "create_time": job["owner_create_time"]})
        release.touch()
        until(lambda: svc.store.get(rid)["state"] == "waiting_approval")
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


def test_stale_process_marker_does_not_authorize_signal(service):
    svc, _, _ = service
    runtime = WebRuntime(svc)
    runtime.path.write_text(
        json.dumps({"pid": os.getpid(), "create_time": 1, "token": "old", "port": 8000})
    )
    assert runtime.stop() == {"alive": False}
    assert not svc.store.path.with_suffix(".web-stop.json").exists()
    assert os.getpid() > 0
