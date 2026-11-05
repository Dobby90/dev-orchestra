"""Response repair is bounded and never replaces source/test authorization."""

import json
from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.agents.contracts import ResponseError, response_fields
from dev_orchestra.agents.feedback import source_context
from dev_orchestra.config import Codex
from dev_orchestra.domain.models import AgentRequest, AgentResult
from dev_orchestra.infrastructure.codex import CodexClient
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
                    assert "AC1" in request.context["response_feedback"]["error"]
                    assert request.context["response_feedback"]["input_sha"] == request.input_sha
            return result

    agent = Agent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"
    assert agent.calls == ["planner", "developer", "tester", "tester", "reviewer"]
    steps = svc.store.steps(rid)
    assert [s["status"] for s in steps if s["role"] == "tester"] == ["failed", "completed"]
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


def test_patch_repair_receives_current_file(service):
    svc, rid, _ = service

    class Agent(FakeAgent):
        def run(self, request, directory, stop):
            result = super().run(request, directory, stop)
            if request.role == "developer":
                if self.calls.count("developer") == 1:
                    old = "stale"
                else:
                    files = request.context["response_feedback"]["current_files"]
                    assert files == [{"path": "main.py", "content": "original\n"}]
                    old = "original"
                result.patch = f"--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-{old}\n+changed\n"
            return result

    agent = Agent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"
    assert agent.calls.count("developer") == 2
    assert (Path(svc.store.get(rid)["worktree"]) / "main.py").read_text() == "changed\n"


def test_role_schema_normalizes_to_common_artifact(tmp_path, monkeypatch):
    def execute(argv, *args, **kwargs):
        schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
        assert set(schema["properties"]) == {
            "summary",
            "questions",
            "patch",
            "test_changes",
            "no_change_reason",
            "commit_message",
        }
        Path(argv[argv.index("--output-last-message") + 1]).write_text(
            json.dumps(
                {
                    "summary": "already implemented; no change needed",
                    "questions": [],
                    "patch": "",
                    "test_changes": [],
                    "no_change_reason": "source already satisfies requirements",
                }
            )
        )
        return 0, ""

    monkeypatch.setattr("dev_orchestra.infrastructure.codex.execute", execute)
    req = AgentRequest(
        run_id="r", role="developer", worktree=str(tmp_path), iteration=0, input_sha="a", context={}
    )
    result = CodexClient(Codex()).run(req, tmp_path / "attempt")
    assert result.criteria == [] and result.summary
    assert response_fields(req) == {
        "summary",
        "questions",
        "patch",
        "test_changes",
        "no_change_reason",
        "commit_message",
    }


def test_current_context_excludes_secrets_and_untracked(service):
    svc, rid, repo = service
    (repo / ".env").write_text("private")
    (repo / "unknown.py").write_text("private")
    assert source_context(repo, "+++ b/.env\n+++ b/unknown.py\n+++ b/../other") == []


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


def test_noop_requires_explicit_reason_on_new_wire_schema(tmp_path, monkeypatch):
    def execute(argv, *args, **kwargs):
        Path(argv[argv.index("--output-last-message") + 1]).write_text(
            json.dumps(
                {
                    "summary": "ok",
                    "questions": [],
                    "patch": "",
                    "test_changes": [],
                    "no_change_reason": "",
                }
            )
        )
        return 0, ""

    monkeypatch.setattr("dev_orchestra.infrastructure.codex.execute", execute)
    req = AgentRequest(
        run_id="r", role="developer", worktree=str(tmp_path), iteration=0, input_sha="a", context={}
    )
    with pytest.raises(ResponseError):
        CodexClient(Codex()).run(req, tmp_path / "attempt")


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
    assert svc.store.get(rid)["state"] == "failed"
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
                collector = UsageCollector(directory / "telemetry.json", AgentOptions())
                collector.event(
                    {
                        "type": "turn.completed",
                        "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 2},
                    }
                )
                collector.finish(True)
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
