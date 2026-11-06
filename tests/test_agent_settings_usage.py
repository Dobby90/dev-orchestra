"""Agent preferences and host accounting, with no real model or account calls."""

import json
import sys
from pathlib import Path

import pytest
from test_backend_api import project
from test_backend_api import web as web
from test_workflow import service as service

from dev_orchestra.application.agent_settings import AgentSettingsService
from dev_orchestra.application.agent_usage import usage_view
from dev_orchestra.config import AgentSettings, Codex
from dev_orchestra.domain.models import AgentRequest, AgentResult, PipelineError
from dev_orchestra.infrastructure.agent_usage import UsageCollector
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.infrastructure.codex_catalog import list_models

MODELS = [
    {"model": "test-standard", "reasoning_efforts": ["low", "medium", "high"]},
    {"model": "test-review", "reasoning_efforts": ["high", "xhigh"]},
]


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setattr("dev_orchestra.infrastructure.codex_catalog.list_models", lambda _: MODELS)


def test_settings_persist_new_runs_only_and_preserve_execution_policy(web, catalog):
    client, svc, original_id, repo, _ = web
    p = project(client, svc, repo)
    initial = client.get("/api/settings/agents").json()
    settings = {
        "model": "test-standard",
        "reasoning_effort": "medium",
        "roles": {
            "reviewer": {"model": "test-review", "reasoning_effort": "xhigh"},
            "tester": {"model": None, "reasoning_effort": "low"},
        },
    }
    result = client.patch(
        "/api/settings/agents", json={"settings": settings, "expected_version": initial["version"]}
    )
    assert result.status_code == 200, result.text
    assert result.json()["settings"] == settings
    # A separate service sees the file after API restart; user YAML stays untouched.
    assert AgentSettingsService(svc.config).get()["settings"].model == "test-standard"
    assert svc.config.codex.model is None
    assert svc.store.get(original_id)["config"]["codex"]["model"] is None
    proposal = {"project_id": p["id"], "spec": {"kind": "text", "text": "new task"}}
    preview = client.post(f"/api/projects/{p['id']}/run-previews", json=proposal).json()
    response = client.post(
        "/api/runs",
        json={**proposal, "preview_token": preview["preview_token"]},
        headers={"Idempotency-Key": "agent-settings-new-run"},
    )
    assert response.status_code == 202, response.text
    run = svc.store.get(response.json()["run_id"])
    configured = Codex.model_validate(run["config"]["codex"])
    assert configured.for_role("developer").model == "test-standard"
    assert configured.for_role("tester").reasoning_effort == "low"
    assert configured.for_role("reviewer").model == "test-review"
    assert configured.command == svc.config.codex.command
    assert configured.timeout_seconds == svc.config.codex.timeout_seconds
    assert run["config"]["git"] == svc.config.git.model_dump()
    # CLI creation also reads saved workspace defaults; existing runs never re-resolve.
    new_id = svc.create(repo, Path(svc.store.get(original_id)["directory"]) / "spec.md")
    assert svc.store.get(new_id)["config"]["codex"]["model"] == "test-standard"
    assert svc.store.get(original_id)["config"]["codex"]["model"] is None


def test_settings_conflicts_validation_csrf_and_preview_staleness(web, catalog):
    c, svc, _, repo, _ = web
    p = project(c, svc, repo)
    proposal = {"project_id": p["id"], "spec": {"kind": "text", "text": "task"}}
    preview = c.post(f"/api/projects/{p['id']}/run-previews", json=proposal).json()
    current = c.get("/api/settings/agents").json()
    body = {
        "settings": {"model": "test-standard", "reasoning_effort": "high"},
        "expected_version": current["version"],
    }
    assert (
        c.patch("/api/settings/agents", json=body, headers={"X-CSRF-Token": "bad"}).status_code
        == 403
    )
    assert (
        c.patch(
            "/api/settings/agents", json={**body, "settings": {"command": "untrusted"}}
        ).status_code
        == 422
    )
    assert (
        c.patch(
            "/api/settings/agents", json={**body, "settings": {"roles": {"unknown": {}}}}
        ).status_code
        == 422
    )
    assert (
        c.patch(
            "/api/settings/agents",
            json={**body, "settings": {"model": "test-review", "reasoning_effort": "low"}},
        ).status_code
        == 422
    )
    assert (
        c.patch(
            "/api/settings/agents", json={**body, "settings": {"reasoning_effort": "high"}}
        ).status_code
        == 422
    )
    assert c.patch("/api/settings/agents", json=body).status_code == 200
    assert c.patch("/api/settings/agents", json=body).status_code == 412
    response = c.post(
        "/api/runs",
        json={**proposal, "preview_token": preview["preview_token"]},
        headers={"Idempotency-Key": "stale-settings"},
    )
    assert response.status_code == 409
    assert response.json()["code"] == "PREVIEW_STALE"
    # Reads neither start Codex nor mutate the settings or database.
    path = AgentSettingsService(svc.config).path
    before = path.read_bytes()
    c.get("/api/settings/agents")
    assert path.read_bytes() == before


@pytest.mark.parametrize("rc,invalid", [(0, False), (3, False), (0, True)])
@pytest.mark.parametrize("continuing", [False, True])
def test_adapter_options_and_usage_survive_failed_result(tmp_path, rc, invalid, continuing):
    script = tmp_path / "codex-stub"
    script.write_text(f"""#!{sys.executable}
import json,sys
from pathlib import Path
assert sys.argv[sys.argv.index('--model')+1]=='test-review'
assert 'model_reasoning_effort="xhigh"' in sys.argv
assert '--ignore-user-config' in sys.argv
assert sys.argv[sys.argv.index('--sandbox')+1]=='read-only'
sys.stdin.read()
print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":100,"cached_input_tokens":70,"output_tokens":20}}}}))
print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":999,"cached_input_tokens":0,"output_tokens":999}}}}), file=sys.stderr)
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text({("invalid" if invalid else AgentResult(summary="ok").model_dump_json())!r})
sys.exit({rc})
""")
    script.chmod(0o700)
    client = CodexClient(
        Codex(
            command=str(script),
            model="test-standard",
            reasoning_effort="medium",
            roles={"reviewer": {"model": "test-review", "reasoning_effort": "xhigh"}},
        )
    )
    request = AgentRequest(
        run_id="r",
        role="reviewer",
        worktree=str(tmp_path),
        iteration=0,
        input_sha="a",
        context={"workspace_resume": {"continuing": continuing}},
    )
    directory = tmp_path / "attempt"
    if rc or invalid:
        with pytest.raises(PipelineError):
            client.run(request, directory)
    else:
        client.run(request, directory)
    data = json.loads((directory / "telemetry.json").read_text())
    assert data["model"] == "test-review"
    assert data["usage"]["cached_input_tokens"] == 70
    assert data["usage"]["input_tokens"] == 100
    assert data["usage_status"] == ("partial" if rc else "reported")
    metrics = json.loads((directory / "input-metrics.json").read_text())
    assert metrics["prompt_bytes"] > 0
    assert metrics["continuation"] is continuing
    assert not metrics["conversation_resumed"]  # Lost conversations can still be follow-up work.
    assert "private" not in json.dumps(metrics)


def test_usage_retries_unknown_and_read_only_replay(web):
    c, svc, rid, _, _ = web
    run = svc.store.get(rid)
    first, attempt = svc.store.start_step(rid, "developer", 0, run["base_sha"])
    directory = Path(run["directory"]) / "iterations/000/developer" / f"attempt-{attempt:03}"
    collector = UsageCollector(
        directory / "telemetry.json", AgentSettings(model="test-standard").for_role("developer")
    )
    event = {
        "type": "turn.completed",
        "usage": {"input_tokens": 100, "cached_input_tokens": 70, "output_tokens": 20},
    }
    collector.event(event)
    collector.event(event)
    collector.finish(False)
    svc.store.interrupt_steps(rid, "failed attempt")
    second, _ = svc.store.start_step(rid, "developer", 0, run["base_sha"])
    svc.store.interrupt_steps(rid, "missing usage")
    before = svc.store.get(rid)
    a = c.get(f"/api/runs/{rid}/agent-usage")
    assert a.status_code == 200, a.text
    data = a.json()
    assert data["usage"] == {"input_tokens": 200, "cached_input_tokens": 140, "output_tokens": 40}
    assert data["usage_status"] == "partial"
    assert data["measured_attempts"] == 1 and data["total_attempts"] == 2
    assert data["attempts"][0]["step_id"] == first
    assert data["attempts"][1]["step_id"] == second
    assert data["attempts"][1]["usage"] is None
    assert c.get(f"/api/runs/{rid}/agent-usage").json() == data
    assert svc.store.get(rid) == before
    # Unsafe telemetry is ignored; GET never repairs it or follows a symlink.
    (directory / "telemetry.json").unlink()
    (directory / "telemetry.json").symlink_to(Path(run["directory"]) / "config.json")
    assert usage_view(svc.store, rid)["usage"] is None


def test_invalid_and_zero_usage_are_distinct(tmp_path):
    collector = UsageCollector(tmp_path / "telemetry.json", Codex().for_role("planner"))
    collector.event(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1, "cached_input_tokens": 2, "output_tokens": 0},
        }
    )
    collector.finish(True)
    assert collector.value.usage is None
    assert collector.value.usage_status == "unknown"
    clean = UsageCollector(tmp_path / "zero.json", Codex().for_role("planner"))
    clean.event(
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
        }
    )
    clean.finish(True)
    assert clean.value.usage is not None and clean.value.usage_status == "reported"


def test_catalog_uses_only_metadata_rpc_and_handles_pagination(tmp_path):
    script = tmp_path / "codex-metadata"
    script.write_text(f"""#!{sys.executable}
import json,sys
assert sys.argv[1:]==['app-server']
for line in sys.stdin:
    req=json.loads(line)
    assert req['method'] in ['initialize','initialized','model/list']
    if 'id' not in req: continue
    if req['method']=='initialize': result={{}}
    else:
        cursor=req['params'].get('cursor')
        result={{'data':[{{'model':'test-review' if cursor else 'test-standard','supportedReasoningEfforts':[{{'reasoningEffort':'high'}}]}}],'nextCursor':None if cursor else 'next'}}
    print(json.dumps({{'id':req['id'],'result':result}}),flush=True)
""")
    script.chmod(0o700)
    assert list_models(str(script)) == [
        {"model": "test-standard", "reasoning_efforts": ["high"]},
        {"model": "test-review", "reasoning_efforts": ["high"]},
    ]
