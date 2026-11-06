"""Response repair is bounded and never replaces source/test authorization."""

from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.agents.contracts import ResponseError
from dev_orchestra.domain.models import AgentResult
from dev_orchestra.orchestration.runner import Runner


def test_coverage_repair_only_repeats_tester(service):
    svc, rid, _ = service

    class Agent(FakeAgent):
        def run(self, request, directory, stop):
            result = super().run(request, directory, stop)
            if request.role == "tester":
                if self.calls.count("tester") == 1:
                    result.coverage = []
                else:
                    assert "AC1" in request.context["workspace_resume"]["remaining"]
                    assert request.context["workspace_resume"]["metadata_only"]
            return result

    agent = Agent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"
    assert agent.calls == ["planner", "developer", "tester", "tester", "reviewer"]
    steps = svc.store.steps(rid)
    assert [s["status"] for s in steps if s["role"] == "tester"] == ["completed"]
    assert svc.store.get(rid)["iteration"] == 0
    svc.approve(rid)


def test_retry_budget_survives_resume(service):
    svc, rid, _ = service

    class Agent(FakeAgent):
        def run(self, request, directory, stop):
            result = super().run(request, directory, stop)
            if request.role == "tester":
                result.coverage = []
            return result

    agent = Agent()
    runner = Runner(svc.store, agent, FakeTests())
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "needs_input"
    assert agent.calls.count("tester") == 2
    svc.resume(rid)
    runner.execute(rid)
    assert agent.calls.count("tester") == 2
    assert svc.store.get(rid)["state"] == "needs_input"


def test_unexpected_role_fields_rejected():
    from dev_orchestra.agents.contracts import validate_role
    from dev_orchestra.domain.models import Criterion

    with pytest.raises(ResponseError, match="developer must not return criteria"):
        validate_role(
            "developer", AgentResult(summary="ok", criteria=[Criterion(id="A", description="x")])
        )


def test_pending_plan_with_13_criteria_and_legacy_config(service, monkeypatch):
    from dev_orchestra.application.plan_approval import binding, decide
    from dev_orchestra.application.service import RunService
    from dev_orchestra.domain.models import Coverage, Criterion

    svc, old, repo = service
    monkeypatch.undo()
    rid = svc.create(repo, Path(svc.store.get(old)["directory"]) / "spec.md")

    class Agent(FakeAgent):
        def run(self, request, directory, stop):
            result = super().run(request, directory, stop)
            if request.role == "planner":
                result.criteria = [
                    Criterion(id=f"AC{i}", description=f"condition {i}") for i in range(13)
                ]
            if request.role == "tester":
                result.coverage = [
                    Coverage(criterion_id=f"AC{i}", command_ids=["unit"]) for i in range(13)
                ]
            return result

    agent = Agent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_plan_approval"
    original_hash = binding(svc.store, run)["plan_hash"]
    config = run["config"]
    config["workflow"].pop("response_retries", None)
    svc.store.update(rid, config=config)
    reopened = RunService(svc.config)
    assert binding(reopened.store, reopened.store.get(rid))["plan_hash"] == original_hash
    decide(reopened, rid, original_hash, True)
    Runner(reopened.store, agent, FakeTests()).execute(rid)
    assert agent.calls.count("planner") == 1
    assert reopened.store.get(rid)["state"] == "waiting_approval"
    reopened.approve(rid)


def test_changed_source_never_retried(service):
    svc, rid, _ = service

    class Agent(FakeAgent):
        def run(self, request, directory, stop):
            result = super().run(request, directory, stop)
            if request.role == "developer":
                (Path(request.worktree) / "main.py").write_text("external change\n")
                raise ResponseError("bad response")
            return result

    agent = Agent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "needs_input"
    assert agent.calls.count("developer") == 1
    assert (Path(svc.store.get(rid)["worktree"]) / "main.py").read_text() == "external change\n"


def test_interrupted_retry_does_not_spend_another_call(service):
    from dev_orchestra.application.agent_usage import usage_view
    from dev_orchestra.config import AgentOptions
    from dev_orchestra.domain.models import StopRequested
    from dev_orchestra.infrastructure.agent_usage import UsageCollector

    svc, rid, _ = service

    class Agent(FakeAgent):
        def run(self, request, directory, stop):
            result = super().run(request, directory, stop)
            if request.role == "tester":
                collector = UsageCollector(
                    directory
                    / "calls"
                    / f"call-{self.calls.count('tester'):03}"
                    / "telemetry.json",
                    AgentOptions(),
                )
                collector.event(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2},
                    }
                )
                collector.finish(True)
                from dev_orchestra.infrastructure.conversations import aggregate_calls

                aggregate_calls(directory, AgentOptions())
                if self.calls.count("tester") == 1:
                    result.coverage = []
                else:
                    raise StopRequested("pause")
            return result

    agent = Agent()
    runner = Runner(svc.store, agent, FakeTests())
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "paused"
    usage = usage_view(svc.store, rid)
    assert usage["usage"] == {"input_tokens": 20, "cached_input_tokens": 8, "output_tokens": 4}
    svc.resume(rid)
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "needs_input"
    assert agent.calls.count("tester") == 2
