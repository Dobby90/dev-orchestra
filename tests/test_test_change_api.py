from pathlib import Path

import pytest
from test_backend_api import project
from test_backend_api import web as web
from test_test_change_approval import PATCH, TEST_FILE, XmlAgent, create_xml_run, legacy_pending
from test_workflow import FakeTests
from test_workflow import service as service

from dev_orchestra.orchestration.runner import Runner


def pending_web(web):
    client, svc, old, repo, _ = web
    pid = project(client, svc, repo)["id"]
    _, rid, _, agent = legacy_pending(svc, old, repo)
    with svc.store.connection() as db:
        db.execute("UPDATE runs SET project_id=? WHERE id=?", (pid, rid))
    return client, svc, rid, agent


def test_web_approval_is_versioned_idempotent_and_requires_exact_proposal(web):
    client, svc, rid, agent = pending_web(web)
    before = svc.store.get(rid)
    events = svc.store.events(rid)
    detail = client.get(f"/api/runs/{rid}").json()
    assert detail["test_change_pending"] is True
    assert "resume" in detail["allowed_actions"]
    assert "approve-test-changes" not in detail["allowed_actions"]
    proposal = client.get(f"/api/runs/{rid}/test-changes").json()["proposal"]
    assert proposal["patch"] == PATCH
    assert "artifact" not in proposal
    assert svc.store.get(rid) == before and svc.store.events(rid) == events
    body = {"proposal_hash": proposal["proposal_hash"], "input_sha": proposal["input_sha"]}
    url = f"/api/runs/{rid}/approve-test-changes"
    assert client.post(url, json=body).status_code == 428
    headers = {"If-Match": f'"run:{rid}:v{detail["version"]}"', "Idempotency-Key": "approve-exact"}
    assert (
        client.post(url, json=body | {"proposal_hash": "0" * 64}, headers=headers).status_code
        == 409
    )
    response = client.post(url, json=body, headers=headers)
    assert response.status_code == 202, response.text
    assert client.post(url, json=body, headers=headers).json() == response.json()
    assert (
        client.post(url, json=body, headers={**headers, "Idempotency-Key": "new"}).status_code
        == 412
    )
    assert (Path(before["worktree"]) / TEST_FILE).read_text().endswith("False\n")
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    view = client.get(f"/api/runs/{rid}/test-changes").json()
    assert view["proposal"] is None and view["error"] is None
    assert view["history"][0]["patch"] == PATCH


def test_web_legacy_reject_requires_reason_and_regenerates_automatically(web):
    client, svc, rid, agent = pending_web(web)
    run = svc.store.get(rid)
    headers = {"If-Match": f'"run:{rid}:v{run["version"]}"'}
    proposal = run["test_change_proposal"]
    body = {"proposal_hash": proposal["proposal_hash"], "input_sha": proposal["input_sha"]}
    url = f"/api/runs/{rid}/reject-test-changes"
    assert client.post(url, json=body, headers=headers).status_code == 409
    response = client.post(
        url, json=body | {"reason": "기존 회귀 검사를 보존하세요"}, headers=headers
    )
    assert response.status_code == 202, response.text
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"
    assert agent.calls.count("developer") == 2


@pytest.mark.parametrize("action", ["approve-test-changes", "reject-test-changes"])
def test_test_change_decision_requires_csrf(web, action):
    client, svc, rid, _ = pending_web(web)
    p = svc.store.get(rid)["test_change_proposal"]
    del client.headers["X-CSRF-Token"]
    response = client.post(
        f"/api/runs/{rid}/{action}",
        json={"proposal_hash": p["proposal_hash"], "input_sha": p["input_sha"], "reason": "변경"},
        headers={"If-Match": f'"run:{rid}:v{svc.store.get(rid)["version"]}"'},
    )
    assert response.status_code == 403


def test_web_legacy_resume_applies_without_a_test_change_decision(web):
    client, svc, rid, agent = pending_web(web)
    run = svc.store.get(rid)
    response = client.post(
        f"/api/runs/{rid}/resume",
        json={},
        headers={"If-Match": f'"run:{rid}:v{run["version"]}"'},
    )
    assert response.status_code == 202, response.text
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"
    assert not svc.store.get(rid).get("test_change_approvals")


def test_web_automatic_test_history_is_read_only_and_final_approval_is_required(web):
    client, svc, old, repo, _ = web
    pid = project(client, svc, repo)["id"]
    rid = create_xml_run(svc, old, repo)
    with svc.store.connection() as db:
        db.execute("UPDATE runs SET project_id=? WHERE id=?", (pid, rid))
    Runner(svc.store, XmlAgent(), FakeTests()).execute(rid)
    before, events = svc.store.get(rid), svc.store.events(rid)
    detail = client.get(f"/api/runs/{rid}").json()
    assert detail["state"] == "waiting_approval"
    assert detail["test_change_count"] == 1
    assert detail["test_change_pending"] is False
    assert "approve-test-changes" not in detail["allowed_actions"]
    view = client.get(f"/api/runs/{rid}/test-changes").json()
    assert view["proposal"] is None and view["error"] is None
    assert view["history"][0]["patch"] == PATCH
    assert "artifact" not in view["history"][0]
    assert svc.store.get(rid) == before and svc.store.events(rid) == events
