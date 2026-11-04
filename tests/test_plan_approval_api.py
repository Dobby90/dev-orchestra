import pytest
from test_backend_api import project
from test_backend_api import web as web
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.orchestration.runner import Runner


@pytest.mark.parametrize("action", ["approve-plan", "revise-plan"])
def test_plan_api_displays_plan_and_guards_decisions(web, action):
    client, svc, rid, repo, _ = web
    pid = project(client, svc, repo)["id"]
    svc.store.update(rid, plan_approval_required=True)
    with svc.store.connection() as db:
        db.execute("UPDATE runs SET project_id=? WHERE id=?", (pid, rid))
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    response = client.get(f"/api/runs/{rid}")
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["state"] == "waiting_plan_approval"
    assert detail["plan"] == "Implement greeting"
    assert detail["criteria"][0]["id"] == "AC1"
    assert "approve-plan" in detail["allowed_actions"]
    url = f"/api/runs/{rid}/{action}"
    body = {"plan_hash": detail["plan_hash"], "reason": "기획 보완"}
    assert client.post(url, json=body).status_code == 428
    headers = {"If-Match": f'"run:{rid}:v{detail["version"]}"', "Idempotency-Key": "plan-once"}
    assert client.post(url, json=body | {"plan_hash": "0" * 64}, headers=headers).status_code == 409
    result = client.post(url, json=body, headers=headers)
    assert result.status_code == 202, result.text
    assert client.post(url, json=body, headers=headers).json() == result.json()
    assert svc.store.get(rid)["next_role"] == (
        "developer" if action == "approve-plan" else "planner"
    )
