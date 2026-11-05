"""Scope is included in preview/idempotency and fixed in the durable Run."""

import pytest
from test_backend_api import project
from test_backend_api import web as web
from test_workflow import service as service


@pytest.mark.parametrize("release", [False, True])
def test_scope_changes_invalidate_preview_and_idempotency_and_persist(web, monkeypatch, release):
    monkeypatch.undo()
    client, svc, original_id, repo, _ = web
    if release:
        from dev_orchestra.infrastructure.migrations import upgrade

        upgrade(svc.store.path, workspace=True)
    p = project(client, svc, repo)
    body = {"project_id": p["id"], "spec": {"kind": "text", "text": "feature"}}
    url = "/api/projects/" + p["id"] + "/run-previews"
    original = client.post(url, json=body)
    assert original.status_code == 200, original.text
    assert original.json()["test_scope"] == "full"
    affected = {**body, "test_scope": "affected"}
    stale = client.post(
        "/api/runs",
        json={**affected, "preview_token": original.json()["preview_token"]},
        headers={"Idempotency-Key": "stale-scope"},
    )
    assert stale.status_code == 409, stale.text
    receipt = client.post(url, json=affected).json()
    assert receipt["proposal_hash"] != original.json()["proposal_hash"]
    command = {**affected, "preview_token": receipt["preview_token"]}
    submitted = client.post("/api/runs", json=command, headers={"Idempotency-Key": "scope"})
    assert submitted.status_code == 202, submitted.text
    rid = submitted.json()["run_id"]
    assert svc.store.get(rid)["test_scope"] == "affected"
    assert svc.store.get(rid)["execution_contract"] == ("workspace-v2" if release else "patch-v1")
    assert svc.store.get(original_id)["execution_contract"] == "patch-v1"
    assert client.get("/api/runs/" + rid).json()["test_scope"] == "affected"
    assert (
        client.post("/api/runs", json=command, headers={"Idempotency-Key": "scope"}).json()
        == submitted.json()
    )
    conflict = client.post(
        "/api/runs", json={**command, "test_scope": "full"}, headers={"Idempotency-Key": "scope"}
    )
    assert conflict.status_code == 409, conflict.text
    assert client.post(url, json={**body, "test_scope": "none"}).status_code == 422
