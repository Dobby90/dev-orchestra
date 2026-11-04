from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.application.commands import Commands
from dev_orchestra.application.plan_approval import binding, decide
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.orchestration.runner import Runner


@pytest.fixture
def planned(service, monkeypatch):
    svc, old, repo = service
    monkeypatch.undo()
    rid = svc.create(repo, Path(svc.store.get(old)["directory"]) / "spec.md")
    agent = FakeAgent()
    runner = Runner(svc.store, agent, FakeTests())
    runner.execute(rid)
    assert agent.calls == ["planner"], svc.store.get(rid).get("error")
    assert svc.store.get(rid)["state"] == "waiting_plan_approval"
    return svc, rid, agent, runner


def test_approve_then_develop_and_final_approval(planned):
    svc, rid, agent, runner = planned
    run = svc.store.get(rid)
    h = binding(svc.store, run)["plan_hash"]
    runner.execute(rid)
    assert agent.calls == ["planner"], svc.store.get(rid).get("error")
    with pytest.raises(PipelineError):
        svc.resume(rid)
    decide(svc, rid, h, True)
    with pytest.raises(PipelineError):
        decide(svc, rid, h, True)
    runner.execute(rid)
    assert agent.calls.count("developer") == 1
    assert svc.store.get(rid)["state"] == "waiting_approval"
    svc.approve(rid)
    assert svc.store.get(rid)["state"] == "completed"


@pytest.mark.parametrize("upgrade_database", [False, True])
def test_revision_requires_new_approval(planned, upgrade_database):
    svc, rid, agent, runner = planned
    old = binding(svc.store, svc.store.get(rid))["plan_hash"]
    decide(svc, rid, old, False, "예외 처리 계획 보완")
    runner.execute(rid)
    assert agent.calls == ["planner", "planner"]
    assert svc.store.get(rid)["state"] == "waiting_plan_approval"
    with pytest.raises(PipelineError):
        decide(svc, rid, old, True)
    assert svc.store.get(rid)["iteration"] == 0
    new = binding(svc.store, svc.store.get(rid))["plan_hash"]
    if upgrade_database:
        upgrade(svc.store.path)
    assert binding(svc.store, svc.store.get(rid))["plan_hash"] == new
    decide(svc, rid, new, True)
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"
    assert agent.calls.count("developer") == 1


@pytest.mark.parametrize("change", ["plan", "criteria", "answers", "head", "artifact"])
def test_changed_plan_or_worktree_cannot_be_approved(planned, change):
    svc, rid, _, _ = planned
    run = svc.store.get(rid)
    h = binding(svc.store, run)["plan_hash"]
    if change == "head":
        (Path(run["worktree"]) / "main.py").write_text("external edit")
    elif change == "artifact":
        step = svc.store.steps(rid)[-1]
        Path(step["data"]["artifact"]).write_text("{}")
    else:
        svc.store.update(rid, **{change: [] if change == "criteria" else "changed"})
    with pytest.raises(PipelineError):
        decide(svc, rid, h, True)
    assert svc.store.get(rid)["state"] == "waiting_plan_approval"


def test_commands_are_atomic_and_idempotent(planned):
    svc, rid, _, _ = planned
    upgrade(svc.store.path)
    run = svc.store.get(rid)
    body = {"plan_hash": binding(svc.store, run)["plan_hash"]}
    commands = Commands(svc)
    result = commands.execute(rid, "approve-plan", run["version"], body, "approve-once")
    assert commands.execute(rid, "approve-plan", run["version"], body, "approve-once") == result
    with svc.store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE run_id=?", (rid,)).fetchone()[0] == 1


def test_missing_approval_cannot_be_bypassed(planned):
    svc, rid, agent, runner = planned
    svc.store.update(rid, "queued")
    runner.execute(rid)
    assert "developer" not in agent.calls
    assert svc.store.get(rid)["state"] == "failed"


def test_new_instructions_before_development_require_replanning(planned, tmp_path):
    svc, rid, agent, runner = planned
    h = binding(svc.store, svc.store.get(rid))["plan_hash"]
    decide(svc, rid, h, True)
    svc.store.update(rid, "paused")
    answers = tmp_path / "answers.md"
    answers.write_text("추가 요구사항")
    svc.resume(rid, answers)
    runner.execute(rid)
    assert agent.calls == ["planner", "planner"]
    assert svc.store.get(rid)["state"] == "waiting_plan_approval"


def test_cli_displays_hash_and_approves_without_model_call(planned, monkeypatch):
    import json

    from typer.testing import CliRunner

    from dev_orchestra.application.service import RunService
    from dev_orchestra.cli import app

    svc, rid, _, _ = planned
    client = CliRunner()
    options = ["--database", str(svc.store.path)]
    result = client.invoke(app, [*options, "status", rid])
    assert result.exit_code == 0, result.output
    h = json.loads(result.output)["plan_hash"]
    starts = []
    monkeypatch.setattr(
        RunService, "start", lambda self, run_id, detached=False: starts.append(run_id)
    )
    result = client.invoke(app, [*options, "approve-plan", rid, "--plan-hash", h])
    assert result.exit_code == 0, result.output
    assert starts == [rid]
    assert svc.store.get(rid)["next_role"] == "developer"
