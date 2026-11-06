"""Manual delivery and credential isolation against disposable local remotes only."""

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from test_backend_api import project
from test_backend_api import web as web
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.web_contracts import GitSettingsInput
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.git_credentials import credentials, network_git
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.infrastructure.process import execution_scope
from dev_orchestra.orchestration.runner import Runner


class Vault:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value

    def delete(self, key):
        self.values.pop(key, None)


def setup(web, tmp_path):
    client, svc, rid, repo, _ = web
    p = project(client, svc, repo)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    Git(repo).call("remote", "add", "origin", str(remote))
    delivery = client.app.state.delivery
    settings = delivery.view(p["id"])
    delivery.save(p["id"], GitSettingsInput(expected_version=settings["version"], remote="origin"))
    return client, svc, rid, repo, p["id"], remote, delivery


def approve(svc, rid):
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    svc.approve(rid)


def push(client, svc, rid, preview=None):
    preview = preview or client.get(f"/api/runs/{rid}/push").json()
    body = {
        key: preview[key] for key in ["settings_version", "remote_url", "branch", "approved_sha"]
    }
    body["approved_sha"] = body["approved_sha"] or svc.store.get(rid).get("final_sha")
    return client.post(
        f"/api/runs/{rid}/push",
        json=body,
        headers={"If-Match": f'"run:{rid}:v{svc.store.get(rid)["version"]}"'},
    )


def test_push_retry_and_cleanup_preserve_approval_and_original(web, tmp_path):
    c, svc, rid, repo, pid, remote, delivery = setup(web, tmp_path)
    original = Git(repo)
    before = original.sha(), original.dirty(), original.call("branch", "--show-current")
    approve(svc, rid)
    assert Git(remote).call("for-each-ref") == ""  # approval never pushes
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        return {"status": "access_denied", "message": "권한 확인 필요"}

    delivery.transport = fail
    response = push(c, svc, rid)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["status"] == "access_denied"
    assert not svc.store.get(rid).get("worktree_removed")
    delivery.transport = network_git
    response = push(c, svc, rid)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["status"] == "success", response.text
    run = svc.store.get(rid)
    assert Git(remote).call("for-each-ref", "--format=%(refname)") == "refs/heads/" + run["branch"]
    assert Git(remote).baseline(run["branch"]) == run["final_sha"]
    assert len(run["push_attempts"]) == 2
    steps = svc.store.steps(rid)
    # A dirty worktree blocks cleanup, without changing Push success.
    (Path(run["worktree"]) / "preserve.txt").write_text("preserve")
    with pytest.raises(PipelineError):
        svc.cleanup(rid)
    assert delivery.preview(rid)["result"]["status"] == "success"
    (Path(run["worktree"]) / "preserve.txt").unlink()
    svc.cleanup(rid)
    assert svc.store.steps(rid) == steps
    assert original.baseline(run["branch"]) == run["final_sha"]
    assert Path(run["directory"], "final-report.md").is_file()
    assert before == (original.sha(), original.dirty(), original.call("branch", "--show-current"))
    assert delivery.preview(rid)["result"]["status"] == "success"
    assert len(calls) == 1


@pytest.mark.parametrize("tamper", ["sha", "main", "evidence", "unapproved", "preview"])
def test_push_rejects_unapproved_or_changed_candidate(web, tmp_path, tamper):
    c, svc, rid, repo, pid, remote, delivery = setup(web, tmp_path)
    if tamper == "unapproved":
        Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    else:
        approve(svc, rid)
    preview = delivery.preview(rid)
    run = svc.store.get(rid)
    if tamper == "sha":
        git = Git(Path(run["worktree"]))
        (git.repo / "later.py").write_text("later = True")
        git.commit("승인 후 변경")
    elif tamper == "main":
        svc.store.update(rid, branch="main")
    elif tamper == "evidence":
        with svc.store.connection() as db:
            db.execute("DELETE FROM approvals WHERE run_id=?", (rid,))
    elif tamper == "unapproved":
        pass
    else:
        Git(repo).call("remote", "set-url", "origin", str(tmp_path / "changed.git"))
    assert push(c, svc, rid, preview).status_code == 409
    assert Git(remote).call("for-each-ref") == ""


def test_push_cleanup_and_settings_mutation_exclude_each_other(web, tmp_path):
    c, svc, rid, repo, pid, remote, delivery = setup(web, tmp_path)
    approve(svc, rid)
    started, release = Event(), Event()

    def blocked(*args, **kwargs):
        started.set()
        assert release.wait(30)
        return {"status": "success", "message": "완료"}

    delivery.transport = blocked
    with ThreadPoolExecutor() as pool:
        task = pool.submit(push, c, svc, rid)
        try:
            assert started.wait(30)
            with pytest.raises(OwnershipError):
                svc.cleanup(rid)
            with pytest.raises(OwnershipError):
                with lock(Path(svc.store.get(rid)["directory"]) / "execution.lock"):
                    pass
            with pytest.raises(OwnershipError):
                delivery.test(pid, delivery.view(pid)["version"])
        finally:
            release.set()
        assert task.result().status_code == 200


def test_tokens_are_vault_only_replaced_deleted_and_never_returned(web, tmp_path):
    c, svc, rid, repo, pid, remote, delivery = setup(web, tmp_path)
    git = Git(repo)
    git.call("remote", "set-url", "origin", "https://example.invalid/team/repo.git")
    vault = delivery.vault = Vault()
    endpoint = f"/api/projects/{pid}/git"

    def save(token=None, delete=False):
        return c.patch(
            endpoint,
            json={
                "expected_version": delivery.view(pid)["version"],
                "remote": "origin",
                "account": "example-id",
                "token": token,
                "delete_token": delete,
            },
        )

    assert save("CANARY_secret").status_code == 200
    assert list(vault.values.values()) == ["CANARY_secret"]
    assert "CANARY_secret" not in json.dumps(delivery.view(pid))
    assert "CANARY_secret" not in delivery.path(pid).read_text()
    assert save("REPLACEMENT_secret").status_code == 200
    assert list(vault.values.values()) == ["REPLACEMENT_secret"]
    captured = []

    def fake(repo, args, account, token, **kwargs):
        captured.append((args, account, token))
        return {"status": "success", "message": "조회 완료"}

    delivery.transport = fake
    v = delivery.view(pid)
    response = c.post(endpoint + "/test", json={"expected_version": v["version"]})
    assert response.status_code == 200, response.text
    assert response.json()["test"]["push_permission"] == "unknown"
    assert captured[0][0][0] == "ls-remote"
    assert captured[0][1:] == ("example-id", "REPLACEMENT_secret")
    assert "REPLACEMENT_secret" not in response.text
    # Changes outside this app invalidate tests and cannot send the token elsewhere.
    git.call("remote", "set-url", "origin", "https://other.invalid/repo.git")
    assert delivery.view(pid)["test"] is None
    with pytest.raises(ApplicationError):
        delivery.network(pid, "https://other.invalid/repo.git", ["ls-remote"])
    assert save(delete=True).status_code == 200
    assert not vault.values
    assert not delivery.view(pid)["token_registered"]
    assert Git(repo).call("config", "user.name") == "Test"


def test_local_connection_query_creates_no_remote_refs_and_invalidates(web, tmp_path):
    c, svc, rid, repo, pid, remote, delivery = setup(web, tmp_path)
    v = delivery.view(pid)
    result = delivery.test(pid, v["version"])
    assert result["test"]["status"] == "success"
    assert result["test"]["push_permission"] == "unknown"
    assert Git(remote).call("for-each-ref") == ""
    result = delivery.save(
        pid, GitSettingsInput(expected_version=result["version"], remote="origin", account="new-id")
    )
    assert result["test"] is None


@pytest.mark.parametrize(
    "url",
    [
        "https://user:CANARY@example.invalid/x",
        "ext::echo CANARY",
        "https://example.invalid/x?token=CANARY",
    ],
)
def test_unsafe_remote_is_never_returned_or_used(web, tmp_path, url):
    c, svc, rid, repo, pid, remote, delivery = setup(web, tmp_path)
    Git(repo).call("remote", "set-url", "origin", url)
    view = delivery.view(pid)
    assert view["remote_url"] is None
    assert "CANARY" not in json.dumps(view)


def test_broker_supplies_credentials_only_to_selected_https_repository(tmp_path):
    secret = "CANARY_socket_secret"
    with credentials("account", secret, "https://example.invalid/team/repo.git") as options:
        assert secret not in str(options)
        for host, expected in [("example.invalid", True), ("other.invalid", False)]:
            proc = subprocess.run(
                ["git", *options, "credential", "fill"],
                input=f"protocol=https\nhost={host}\npath=team/repo.git\n\n",
                text=True,
                capture_output=True,
                env={"PATH": __import__("os").environ["PATH"], "GIT_TERMINAL_PROMPT": "0"},
                timeout=5,
            )
            assert (secret in proc.stdout) == expected


@pytest.mark.parametrize(
    "message,status",
    [
        ("fatal: Authentication failed CANARY", "authentication_failed"),
        ("fatal: repository not found CANARY", "access_denied"),
        ("fatal: Could not resolve host CANARY", "network_error"),
        ("[rejected] non-fast-forward CANARY", "rejected"),
    ],
)
def test_transport_suppresses_all_remote_output(monkeypatch, tmp_path, message, status):
    from dev_orchestra.infrastructure import git_credentials

    def execute(argv, repo, timeout, log, **kwargs):
        assert "CANARY" not in str(argv)
        log.parent.mkdir(parents=True)
        log.write_text(kwargs["output_redactor"](message))
        return 1, ""

    monkeypatch.setattr(git_credentials, "execute", execute)
    with execution_scope(tmp_path):
        result = network_git(tmp_path, ["ls-remote", "https://example.invalid/repo.git"])
    assert result["status"] == status
    assert "CANARY" not in json.dumps(result)
    assert all("CANARY" not in p.read_text() for p in tmp_path.rglob("*.log"))


def test_real_transport_rejects_non_fast_forward_without_overwriting_remote(service, tmp_path):
    _, _, repo = service
    remote = tmp_path / "transport.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    sha = Git(repo).sha()
    args = [
        "push",
        "--porcelain",
        "--no-follow-tags",
        "--recurse-submodules=no",
        "--",
        str(remote),
        sha + ":refs/heads/feature/manual",
    ]
    with execution_scope(tmp_path / "transport"):
        assert network_git(repo, args)["status"] == "success"
    git = Git(remote)
    tree = git.call("rev-parse", sha + "^{tree}")
    newer = git.call(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit-tree",
        tree,
        "-p",
        sha,
        input="원격에서 진행한 작업\n",
    )
    git.call("update-ref", "refs/heads/feature/manual", newer)
    with execution_scope(tmp_path / "transport"):
        assert network_git(repo, args)["status"] == "rejected"
    assert git.baseline("feature/manual") == newer
    assert git.call("for-each-ref", "--format=%(refname)") == "refs/heads/feature/manual"


def test_vault_failure_preserves_registered_token_settings(web, tmp_path):
    c, svc, rid, repo, pid, remote, delivery = setup(web, tmp_path)
    Git(repo).call("remote", "set-url", "origin", "https://example.invalid/repo.git")
    vault = delivery.vault = Vault()
    view = delivery.view(pid)
    delivery.save(
        pid,
        GitSettingsInput(
            expected_version=view["version"], remote="origin", account="example", token="CANARY"
        ),
    )
    before = delivery.path(pid).read_text()
    original_delete = vault.delete

    def denied(key):
        raise ApplicationError("VAULT_UNAVAILABLE", "모의 저장소 잠금")

    vault.delete = denied
    with pytest.raises(ApplicationError):
        delivery.save(
            pid,
            GitSettingsInput(
                expected_version=delivery.view(pid)["version"],
                remote="origin",
                account="example",
                delete_token=True,
            ),
        )
    assert delivery.path(pid).read_text() == before
    assert list(vault.values.values()) == ["CANARY"]
    vault.delete = original_delete
    delivery.save(
        pid,
        GitSettingsInput(
            expected_version=delivery.view(pid)["version"],
            remote="origin",
            account="example",
            delete_token=True,
        ),
    )
    assert not vault.values
