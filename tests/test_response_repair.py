"""Response repair is bounded and never replaces source/test authorization."""

from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service
from test_workspace import edit

from dev_orchestra.agents.contracts import ResponseError
from dev_orchestra.agents.prompts import continuation_prompt
from dev_orchestra.application.results import Results
from dev_orchestra.application.reviews import Reviews
from dev_orchestra.domain.models import AgentResult, FixIssueOutcome
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


@pytest.mark.parametrize(
    ("invalid_id", "correct_on_retry"),
    [("unknown", True), ("duplicate", True), ("unknown", False)],
)
def test_review_assessment_id_repair(service, invalid_id, correct_on_retry):
    svc, rid, _ = service

    class Agent(FakeAgent):
        def run(self, request, directory, stop):
            result = super().run(request, directory, stop)
            if request.role != "reviewer" or request.iteration != 1:
                return result
            assessment = result.assessments[0]
            feedback = request.context.get("response_feedback")
            if feedback:
                assert feedback["input_sha"] == request.input_sha
                assert "unknown or duplicate review assessment" in feedback["error"]
                assert "open_issues" in feedback["error"]
                if correct_on_retry:
                    return result
            if invalid_id == "unknown":
                invalid = assessment.model_copy(update={"issue_id": "issue-unregistered"})
                result.assessments.append(invalid)
            else:
                result.assessments.append(assessment)
            return result

    agent = Agent(block=1)
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["iteration"] == 1
    assert agent.calls == [
        "planner",
        "developer",
        "tester",
        "reviewer",
        "fixer",
        "tester",
        "reviewer",
        "reviewer",
    ]
    steps = [
        step
        for step in svc.store.steps(rid)
        if step["role"] == "reviewer" and step["iteration"] == 1
    ]
    assert [step["status"] for step in steps] == [
        "failed",
        "completed" if correct_on_retry else "failed",
    ]
    assert {step["input_sha"] for step in steps} == {run["current_sha"]}
    with svc.store.connection() as db:
        saved = [row["step_id"] for row in db.execute("SELECT step_id FROM issue_assessments")]
    assert saved == ([steps[-1]["id"]] if correct_on_retry else [])
    assert run["state"] == ("waiting_approval" if correct_on_retry else "needs_input")
    if not correct_on_retry:
        assert run["error"].startswith("response repair budget exhausted:")


def test_unexpected_role_fields_rejected():
    from dev_orchestra.agents.contracts import validate_role
    from dev_orchestra.domain.models import Criterion

    with pytest.raises(ResponseError, match="developer must not return criteria"):
        validate_role(
            "developer", AgentResult(summary="ok", criteria=[Criterion(id="A", description="x")])
        )


@pytest.mark.parametrize(
    "problem,status,recovers",
    [
        ("missing", "implemented", True),
        ("duplicate", "partial", True),
        ("unknown", "blocked", True),
        ("missing", "implemented", False),
    ],
)
def test_fixer_outcomes_are_validated_before_testing(service, problem, status, recovers):
    svc, rid, _ = service

    class Agent(FakeAgent):
        def run_workspace(self, request, directory, session, stop):
            result = super().run_workspace(request, directory, session, stop)
            if request.role != "fixer":
                if request.role == "tester" and request.iteration == 1:
                    assert len(Reviews(svc.store).open(rid)) == 1
                return result

            (issue,) = request.context["open_issues"]
            issue_id = issue["issue_id"]
            assert request.context["response_contract"]["issue_ids"] == [issue_id]
            self.outcome = FixIssueOutcome(
                issue_id=issue_id,
                status=status,
                evidence="main.py의 반환값을 변경했습니다. 남은 조건은 검증이 필요합니다.",
            )
            state = request.context["workspace_resume"]
            if self.calls.count("fixer") == 1:
                edit(session, "main.py", "fixed\n")
            else:
                assert state["metadata_only"]
                assert f"{problem} IDs" in state["remaining"]
                assert issue_id in continuation_prompt(request)
                assert (session.root / "main.py").read_text() == "fixed\n"
                assert session.git.sha() == request.input_sha
                if recovers:
                    result.issue_outcomes = [self.outcome]
                    return result

            result.issue_outcomes = {
                "missing": [],
                "duplicate": [self.outcome, self.outcome],
                "unknown": [
                    self.outcome,
                    self.outcome.model_copy(update={"issue_id": "issue-not-requested"}),
                ],
            }[problem]
            return result

    agent = Agent(block=1)
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["iteration"] == 1
    assert agent.calls[:5] == ["planner", "developer", "tester", "reviewer", "fixer"]
    (step,) = [s for s in svc.store.steps(rid) if s["role"] == "fixer"]
    if recovers:
        assert agent.calls[5:] == ["fixer", "tester", "reviewer"]
        assert run["state"] == "waiting_approval", run.get("error")
        payload = Results(svc.store).payload(run, step)
        assert payload["issue_outcomes"] == [agent.outcome.model_dump()]
        assert payload["sha"] == run["current_sha"]
        assert len(payload["workspace_commits"]) == 1
        svc.approve(rid)
    else:
        assert agent.calls[5:] == ["fixer"]
        assert run["state"] == "needs_input"
        assert "workspace response repair budget exhausted" in run["error"]
        assert "artifact" not in step["data"]
        assert (Path(run["worktree"]) / "main.py").read_text() == "fixed\n"


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
