"""Real SQLite/Git and REST, without Codex calls."""

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_workflow import service as service

from dev_orchestra.api.main import create_app
from dev_orchestra.api.security import bootstrap
from dev_orchestra.domain.models import PipelineError, State
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.infrastructure.store import Store


@pytest.fixture
def web(service):
    svc, rid, repo = service
    backup = upgrade(svc.store.path)
    app = create_app(svc.config, [repo.parent.resolve()])
    client = TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50000),
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    code = bootstrap(svc.store)
    response = client.post("/api/session", json={"bootstrap_code": code})
    assert response.status_code == 201, response.text
    assert "T" in response.json()["expires_at"]
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    yield client, svc, rid, repo, backup
    client.close()


def project(client, svc, repo):
    response = client.post(
        "/api/projects",
        json={
            "name": "테스트",
            "repository_path": str(repo.resolve()),
            "policy": {"tests": svc.config.tests.model_dump(mode="json")},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_migration_preserves_backup_and_cli(web):
    c, svc, rid, repo, backup = web
    old = sqlite3.connect(backup)
    assert old.execute("SELECT version FROM schema_version").fetchone() == (2,)
    assert old.execute("SELECT id FROM runs").fetchone() == (rid,)
    old.close()
    reopened = Store(svc.store.path)
    assert reopened.get(rid) == svc.store.get(rid)
    with reopened.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 5
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert upgrade(svc.store.path) is None


def test_transaction_rolls_back_all_records(web):
    _, svc, rid, _, _ = web
    before = svc.store.get(rid)
    with pytest.raises(ValueError):
        with svc.store.transaction():
            svc.store.control(rid, "pause")
            svc.store.update(rid, "planning")
            raise ValueError("injected")
    assert svc.store.get(rid) == before
    assert svc.store.events(rid) == []


def test_upgrade_refuses_execution_lock(service):
    svc, rid, _ = service
    with lock(Path(svc.store.get(rid)["directory"]) / "execution.lock"):
        with pytest.raises(PipelineError):
            upgrade(svc.store.path)
    with svc.store.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 2


def test_project_crud_version_archive(web):
    c, svc, _, repo, _ = web
    p = project(c, svc, repo)
    pid = p["id"]
    assert c.get("/api/projects").json()["items"][0]["id"] == pid
    assert c.get(f"/api/projects/{pid}/branches").json()["items"] == ["main"]
    assert c.patch(f"/api/projects/{pid}", json={"name": "변경"}).status_code == 428
    headers = {"If-Match": f'"project:{pid}:v0"'}
    assert (
        c.patch(f"/api/projects/{pid}", json={"name": "변경"}, headers=headers).status_code == 200
    )
    assert (
        c.patch(f"/api/projects/{pid}", json={"name": "다시"}, headers=headers).status_code == 412
    )
    assert (
        c.delete(f"/api/projects/{pid}", headers={"If-Match": f'"project:{pid}:v1"'}).status_code
        == 200
    )
    assert c.get("/api/projects").json()["items"] == []
    assert repo.is_dir()


def test_project_exposes_configured_branch_prefix(web):
    c, svc, _, repo, _ = web
    svc.config.project.branch_prefix = "custom/tasks"
    p = project(c, svc, repo)
    assert p["branch_prefix"] == "custom/tasks"
    assert c.get(f"/api/projects/{p['id']}").json()["branch_prefix"] == "custom/tasks"
    assert c.get("/api/projects").json()["items"][0]["branch_prefix"] == "custom/tasks"


def test_project_duplicate_path_denied(web):
    c, svc, _, repo, _ = web
    project(c, svc, repo)
    r = c.post(
        "/api/projects",
        json={
            "name": "중복",
            "repository_path": str(repo.resolve()),
            "policy": {"tests": svc.config.tests.model_dump(mode="json")},
        },
    )
    assert r.status_code == 409


def test_submission_atomic_idempotent_and_not_started(web):
    c, svc, _, repo, _ = web
    before = (repo / "main.py").read_bytes()
    p = project(c, svc, repo)
    proposal = {"project_id": p["id"], "spec": {"kind": "text", "text": "인사말 구현"}}
    preview = c.post(f"/api/projects/{p['id']}/run-previews", json=proposal)
    assert preview.status_code == 200, preview.text
    body = {**proposal, "preview_token": preview.json()["preview_token"]}
    headers = {"Idempotency-Key": "request-one"}
    response = c.post("/api/runs", json=body, headers=headers)
    assert response.status_code == 202, response.text
    result = response.json()
    rid = result["run_id"]
    assert c.post("/api/runs", json=body, headers=headers).json() == result
    assert (
        c.post(
            "/api/runs", json={**body, "additional_instructions": "다른 요청"}, headers=headers
        ).status_code
        == 409
    )
    assert svc.status(rid)["state"] == "queued"
    assert not Path(svc.store.get(rid)["worktree"]).exists()
    assert (repo / "main.py").read_bytes() == before
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 1
    detail = c.get("/api/runs/" + rid)
    assert detail.json()["event_cursor"] > 0
    assert detail.json()["current_role"] == "planner"
    assert "config" not in detail.json() and "spec" not in detail.json()
    assert (
        c.post(
            f"/api/runs/{rid}/cancel", json={}, headers={"If-Match": detail.headers["etag"]}
        ).status_code
        == 202
    )
    assert svc.status(rid)["state"] == "cancelled"


def test_stale_preview_rejected(web):
    c, svc, _, repo, _ = web
    p = project(c, svc, repo)
    proposal = {"project_id": p["id"], "spec": {"kind": "text", "text": "PRD"}}
    token = c.post(f"/api/projects/{p['id']}/run-previews", json=proposal).json()["preview_token"]
    c.patch(
        "/api/projects/" + p["id"],
        json={"name": "changed"},
        headers={"If-Match": f'"project:{p["id"]}:v0"'},
    )
    assert (
        c.post(
            "/api/runs",
            json={**proposal, "preview_token": token},
            headers={"Idempotency-Key": "stale"},
        ).status_code
        == 409
    )
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Origin": "https://evil.example"}, 403),
        ({"Host": "evil.example"}, 403),
        ({"X-CSRF-Token": "invalid"}, 403),
    ],
)
def test_web_security_rejects_writes(web, headers, status):
    c, _, _, _, _ = web
    assert c.post("/api/projects", json={}, headers=headers).status_code == status


def test_session_replay_and_expiry(web):
    c, svc, _, _, _ = web
    code = bootstrap(svc.store)
    assert c.post("/api/session", json={"bootstrap_code": code}).status_code == 201
    assert c.post("/api/session", json={"bootstrap_code": code}).status_code == 401
    with svc.store.connection() as db:
        db.execute("UPDATE web_sessions SET expires_at=0")
    assert c.get("/api/runs").status_code == 401


def freeze_auth_clock(monkeypatch):
    import time
    from types import SimpleNamespace

    from dev_orchestra.api import security

    clock = SimpleNamespace(now=2_000_000_000.0)
    monkeypatch.setattr(
        security, "time", SimpleNamespace(time=lambda: clock.now, monotonic=time.monotonic)
    )
    return clock


def test_bootstrap_five_minute_boundary_and_single_use(web, monkeypatch):
    from dev_orchestra.api.security import digest

    c, svc, _, _, _ = web
    clock = freeze_auth_clock(monkeypatch)
    issued_at = clock.now
    valid, expired = bootstrap(svc.store), bootstrap(svc.store)
    with svc.store.connection() as db:
        assert (
            db.execute(
                "SELECT expires_at FROM web_bootstrap_codes WHERE code_hash=?", (digest(valid),)
            ).fetchone()[0]
            == issued_at + 300
        )
    clock.now = issued_at + 299
    assert c.post("/api/session", json={"bootstrap_code": valid}).status_code == 201
    assert c.post("/api/session", json={"bootstrap_code": valid}).status_code == 401
    clock.now = issued_at + 300
    assert c.post("/api/session", json={"bootstrap_code": expired}).status_code == 401


def test_session_cookie_and_server_expire_after_two_days_without_sliding(web, monkeypatch):
    from datetime import datetime
    from http.cookies import SimpleCookie

    from dev_orchestra.api.security import COOKIE, digest

    c, svc, _, _, _ = web
    clock = freeze_auth_clock(monkeypatch)
    issued_at = clock.now
    response = c.post("/api/session", json={"bootstrap_code": bootstrap(svc.store)})
    assert response.status_code == 201
    cookie = SimpleCookie(response.headers["set-cookie"])[COOKIE]
    assert cookie["max-age"] == "172800"
    assert cookie["httponly"] and cookie["samesite"] == "strict" and cookie["path"] == "/api"
    assert not cookie["domain"]
    token = c.cookies.get(COOKIE)
    expires = response.json()["expires_at"]
    assert datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp() == issued_at + 172800
    for elapsed in (9 * 3600, 172799):
        clock.now = issued_at + elapsed
        checked = c.get("/api/session")
        assert checked.status_code == 200
        assert checked.json()["expires_at"] == expires
    clock.now = issued_at + 172800
    assert c.get("/api/session").status_code == 401
    # The test client still sends its cookie: rejection is enforced by the server.
    assert c.cookies.get(COOKIE) == token
    with svc.store.connection() as db:
        assert (
            db.execute(
                "SELECT expires_at FROM web_sessions WHERE session_hash=?", (digest(token),)
            ).fetchone()[0]
            == issued_at + 172800
        )


def test_two_day_session_survives_restart_but_logout_revokes_it(web, monkeypatch):
    from dev_orchestra.api.security import COOKIE, digest

    c, svc, _, repo, _ = web
    clock = freeze_auth_clock(monkeypatch)
    response = c.post("/api/session", json={"bootstrap_code": bootstrap(svc.store)})
    assert response.status_code == 201
    token = c.cookies.get(COOKIE)
    clock.now += 9 * 3600
    with TestClient(
        create_app(svc.config, [repo.parent.resolve()]),
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50000),
        headers={"Origin": "http://127.0.0.1:8000", "X-CSRF-Token": response.json()["csrf_token"]},
        cookies=c.cookies,
    ) as restarted:
        assert restarted.get("/api/session").status_code == 200
        logout = restarted.delete("/api/session")
        assert logout.status_code == 204 and "Max-Age=0" in logout.headers["set-cookie"]
        assert (
            restarted.get("/api/runs", headers={"Cookie": f"{COOKIE}={token}"}).status_code == 401
        )
    with svc.store.connection() as db:
        assert not db.execute(
            "SELECT 1 FROM web_sessions WHERE session_hash=?", (digest(token),)
        ).fetchone()


def test_upload_and_validation_errors_do_not_echo_input(web):
    c, _, _, _, _ = web
    assert (
        c.post(
            "/api/specs", files={"file": ("prd.md", "한글 PRD".encode(), "text/markdown")}
        ).status_code
        == 201
    )
    assert (
        c.post("/api/specs", files={"file": ("bad.md", b"\xff", "text/markdown")}).status_code
        == 422
    )
    r = c.post("/api/projects", json={"name": "secret_CANARY", "repository_path": 1})
    assert r.status_code == 422 and "secret_CANARY" not in r.text
    assert c.get("/api/openapi.json").status_code == 200


def test_unregistered_cli_run_read_only(web):
    c, svc, rid, _, _ = web
    assert c.get("/api/runs/" + rid).status_code == 200
    response = c.post(
        "/api/runs/" + rid + "/cancel", json={}, headers={"If-Match": f'"run:{rid}:v0"'}
    )
    assert response.status_code == 409
    assert svc.store.get(rid)["state"] == "queued"


def test_session_survives_api_recreation(web):
    c, svc, _, repo, _ = web
    app = create_app(svc.config, [repo.parent.resolve()])
    with TestClient(
        app,
        base_url="http://127.0.0.1:8000",
        client=("127.0.0.1", 50000),
        headers={"Origin": "http://127.0.0.1:8000"},
        cookies=c.cookies,
    ) as restarted:
        assert restarted.get("/api/session").status_code == 200


def test_submission_sql_failure_rolls_back(web, monkeypatch):
    c, svc, _, repo, _ = web
    p = project(c, svc, repo)
    proposal = {"project_id": p["id"], "spec": {"kind": "text", "text": "PRD"}}
    token = c.post(f"/api/projects/{p['id']}/run-previews", json=proposal).json()["preview_token"]

    def fail(*args):
        raise RuntimeError("injected persistence failure")

    monkeypatch.setattr(c.app.state.backend.store, "_event", fail)
    with pytest.raises(RuntimeError, match="injected"):
        c.post(
            "/api/runs",
            json={**proposal, "preview_token": token},
            headers={"Idempotency-Key": "failure"},
        )
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM runs").fetchone()[0] == 1


def test_migration_ddl_failure_rolls_back(service, monkeypatch):
    import dev_orchestra.infrastructure.migrations as migration

    svc, rid, _ = service
    monkeypatch.setattr(migration, "SCHEMA", migration.SCHEMA + ";INVALID SQL;")
    with pytest.raises(sqlite3.OperationalError):
        upgrade(svc.store.path)
    with svc.store.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 2
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='projects'").fetchone()
        assert db.execute("SELECT id FROM runs").fetchone()[0] == rid


def test_rate_limit_and_unauthenticated_access(web):
    c, _, _, _, _ = web
    for _ in range(4):
        assert c.post("/api/session", json={"bootstrap_code": "a" * 43}).status_code == 401
    assert c.post("/api/session", json={"bootstrap_code": "a" * 43}).status_code == 429
    c.cookies.clear()
    assert c.get("/api/runs").status_code == 401
    assert c.get("/api/openapi.json").status_code == 401


def test_large_body_rejected_before_storage(web):
    c, _, _, _, _ = web
    r = c.post(
        "/api/projects",
        content=b"x" * (2 * 1048576 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413


def test_native_http_restart(web, tmp_path):
    import socket
    import subprocess
    import sys
    import time

    import httpx
    import yaml

    c, svc, _, repo, _ = web
    settings = tmp_path / "server.yaml"
    settings.write_text(yaml.safe_dump(svc.config.model_dump(mode="json")))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    script = "from pathlib import Path;import sys,uvicorn;from dev_orchestra.config import load_config;from dev_orchestra.api.main import create_app;uvicorn.run(create_app(load_config(Path(sys.argv[1])),[Path(sys.argv[2])],int(sys.argv[3])),host='127.0.0.1',port=int(sys.argv[3]),proxy_headers=False,log_level='error')"
    headers = {"Origin": f"http://127.0.0.1:{port}"}
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=3) as remote:
        for attempt in range(2):
            worker = subprocess.Popen(
                [sys.executable, "-c", script, str(settings), str(repo.parent), str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        if remote.get("/api/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() > deadline:
                        pytest.fail("HTTP server did not start")
                    time.sleep(0.05)
                if attempt == 0:
                    result = remote.post(
                        "/api/session", json={"bootstrap_code": bootstrap(svc.store)}
                    )
                    assert result.status_code == 201
                assert remote.get("/api/runs").status_code == 200
            finally:
                worker.terminate()
                try:
                    worker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    worker.wait()


def test_project_default_base_is_snapshotted(web):
    from dev_orchestra.infrastructure.git import Git

    c, svc, _, repo, _ = web
    Git(repo).call("branch", "develop")
    p = c.post(
        "/api/projects",
        json={
            "name": "develop project",
            "repository_path": str(repo.resolve()),
            "default_base_branch": "develop",
            "policy": {"tests": svc.config.tests.model_dump(mode="json")},
        },
    ).json()
    proposal = {"project_id": p["id"], "spec": {"kind": "text", "text": "PRD"}}
    receipt = c.post(f"/api/projects/{p['id']}/run-previews", json=proposal).json()
    result = c.post(
        "/api/runs",
        json={**proposal, "preview_token": receipt["preview_token"]},
        headers={"Idempotency-Key": "default-develop"},
    )
    assert result.status_code == 202, result.text
    assert svc.store.get(result.json()["run_id"])["base"] == "develop"


def test_missing_repository_is_safe_input_error(web):
    c, svc, _, repo, _ = web
    response = c.post(
        "/api/projects",
        json={
            "name": "missing",
            "repository_path": str(repo / "missing-private-path"),
            "policy": {"tests": svc.config.tests.model_dump(mode="json")},
        },
    )
    assert response.status_code == 422
    assert "missing-private-path" not in response.text


def test_ui_public_projection_is_read_only_and_typed(web):
    client, svc, rid, _, _ = web
    svc.store.update(rid, error="token=CANARY")
    before = svc.store.events(rid)
    response = client.get(f"/api/runs/{rid}")
    assert response.status_code == 200
    value = response.json()
    assert value["base_branch"] == "main"
    assert value["created_at"]
    assert value["max_fix_attempts"] == svc.config.workflow.max_fix_attempts
    assert "CANARY" not in value["error"]
    assert not {"config", "spec", "directory", "repo", "answers"} & value.keys()
    assert svc.store.events(rid) == before
    schemas = client.get("/api/openapi.json").json()["components"]["schemas"]
    assert set(schemas["State"]["enum"]) == {s.value for s in State}
    assert {
        "RunDetail",
        "RunPage",
        "StepPage",
        "PreviewView",
        "EventPage",
        "LogPage",
    } <= schemas.keys()


def test_ui_legacy_missing_metadata_is_unknown_not_fabricated(web):
    client, svc, _, _, _ = web
    svc.store.create({"id": "legacy-minimal"})
    response = client.get("/api/runs/legacy-minimal")
    assert response.status_code == 200
    value = response.json()
    assert value["base_branch"] is None
    assert value["created_at"] is None
    assert value["max_fix_attempts"] is None
    assert value["project_id"] is None
    assert svc.store.events("legacy-minimal") == []


def test_notifications_transitions_pagination_and_persistent_read(web):
    from dev_orchestra.application.notifications import NotificationService

    c, svc, rid, _, _ = web
    svc.store.update(rid, "planning")
    svc.store.update(rid, "needs_input", questions=["secret question"])
    svc.store.update(rid, "needs_input", error="secret error")
    first = c.get("/api/notifications").json()
    assert first["unread_count"] == 1
    assert len(first["items"]) == 1
    item = first["items"][0]
    assert item["state"] == "needs_input"
    assert item["run_id"] == rid
    assert "secret" not in str(first)
    assert not item["read"]
    assert not svc.store.path.with_suffix(".notifications.json").exists()
    version = svc.store.get(rid)["version"]
    assert c.post("/api/notifications/read", json={"ids": [item["id"]]}).status_code == 204
    assert NotificationService(svc.store).page()["unread_count"] == 0
    assert c.get("/api/notifications").json()["items"][0]["read"]
    svc.store.update(rid, "queued")
    svc.store.update(rid, "planning")
    svc.store.update(rid, "needs_input")
    second = c.get("/api/notifications?limit=1").json()
    assert second["unread_count"] == 1
    assert second["items"][0]["id"] > item["id"]
    older = c.get(f"/api/notifications?before={second['next_cursor']}").json()
    assert older["items"] == [item | {"read": True}]
    # Reading an earlier snapshot must preserve later arrivals as unread.
    assert c.post("/api/notifications/read", json={"through": first["cursor"]}).status_code == 204
    assert c.get("/api/notifications").json()["unread_count"] == 1
    assert c.post("/api/notifications/read", json={"through": second["cursor"]}).status_code == 204
    assert NotificationService(svc.store).page()["unread_count"] == 0
    assert svc.store.get(rid)["version"] == version + 3
    assert c.post("/api/notifications/read", json={"ids": [-1]}).status_code == 422
    assert c.get("/api/notifications?limit=101").status_code == 422
    token = c.headers.pop("X-CSRF-Token")
    assert c.post("/api/notifications/read", json={"through": second["cursor"]}).status_code == 403
    c.headers["X-CSRF-Token"] = token


def test_notifications_include_all_runs_not_only_first_page(web):
    c, svc, rid, _, _ = web
    for number in range(55):
        other = f"notification-{number:03}"
        svc.store.create({"id": other})
        svc.store.update(other, "paused")
    page = c.get("/api/notifications").json()
    assert page["unread_count"] == 55
    assert len(page["items"]) == 50
    assert len(c.get(f"/api/notifications?before={page['next_cursor']}").json()["items"]) == 5
    svc.store.update(rid, "failed")
    assert c.get("/api/notifications").json()["items"][0]["run_id"] == rid


def test_log_window_contract_and_authentication(web):
    from dev_orchestra.infrastructure.log_index import LogIndexer

    c, svc, rid, _, _ = web
    root = Path(svc.store.get(rid)["directory"])
    (root / "worker.log").write_text("처음\n최신 token=CANARY\n")
    LogIndexer(svc.store).index(rid)
    response = c.get(f"/api/runs/{rid}/logs/window?limit=1")
    assert response.status_code == 200
    page = response.json()
    assert page["has_more"]
    assert len(page["items"]) == 1
    assert "CANARY" not in response.text and "[REDACTED]" in response.text
    previous = c.get(f"/api/runs/{rid}/logs/window?before={page['before_cursor']}").json()
    assert previous["items"][0]["data"]["text"] == "처음\n"
    assert not previous["has_more"]
    assert c.get(f"/api/runs/{rid}/logs/window?before=-1").status_code == 422
    assert c.get(f"/api/runs/{rid}/logs/window?step_id=0").status_code == 422
    c.cookies.clear()
    assert c.get(f"/api/runs/{rid}/logs/window").status_code == 401


def test_run_list_exposes_request_and_completion_times(web):
    client, svc, rid, _, _ = web
    created = svc.store.get(rid)["created_at"]
    before = client.get("/api/runs").json()["items"][0]
    assert before["created_at"] == created
    assert before["completed_at"] is None
    with svc.store.connection() as db:
        db.execute("UPDATE runs SET state='waiting_approval' WHERE id=?", (rid,))
    svc.store.approve(rid, "a" * 40, "test evidence")
    completed = svc.store.events(rid)[-1]["timestamp"]
    summary = client.get("/api/runs?state=completed").json()["items"][0]
    assert summary["created_at"] == created
    assert summary["completed_at"] == completed
    detail = client.get(f"/api/runs/{rid}")
    assert detail.status_code == 200
    assert detail.json()["completed_at"] == completed
