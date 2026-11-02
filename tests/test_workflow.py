import subprocess
import pytest
from dev_orchestra.application.service import RunService
from dev_orchestra.config import Config
from dev_orchestra.domain.models import AgentResult, Criterion, Coverage, Issue, PipelineError
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.orchestration.runner import Runner


class FakeAgent:
    def __init__(self, block=0):
        self.calls = []
        self.block = block

    def run(self, req, directory, stop):
        self.calls.append(req.role)
        if req.role == "planner":
            return AgentResult(summary="plan", plan="Implement greeting", criteria=[Criterion(id="AC1", description="greeting works")])
        if req.role == "tester":
            return AgentResult(summary="tests", command_ids=["unit"], coverage=[Coverage(criterion_id="AC1", command_ids=["unit"])])
        if req.role == "reviewer" and req.iteration < self.block:
            return AgentResult(summary="bug", issues=[Issue(id="I1", severity="high", file="main.py", line=1, description="bug", evidence="main.py returns wrong value", suggestion="fix value")])
        return AgentResult(summary="ok")


class FakeTests:
    def run(self, *args):
        return {"passed": True, "commands": [{"exit_code": 0, "total": 1}]}


@pytest.fixture
def service(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git = Git(repo)
    git.call("config", "user.name", "Test")
    git.call("config", "user.email", "test@example.invalid")
    (repo / "main.py").write_text("original\n")
    git.commit("initial")
    # Original developer changes must survive creation and execution.
    (repo / "main.py").write_text("developer local edits\n")
    spec = tmp_path / "PRD.md"
    spec.write_text("Implement greeting")
    cfg = Config.model_validate({"storage": {"database": str(tmp_path / "state.db"), "runs_directory": str(tmp_path / "runs"), "worktrees_directory": str(tmp_path / "worktrees")}, "tests": {"commands": {"unit": {"argv": ["pytest"], "junit_report": "build/results.xml"}}}})
    svc = RunService(cfg)
    run_id = svc.create(repo, spec)
    return svc, run_id, repo


def test_success_and_approval(service):
    svc, run_id, repo = service
    agent = FakeAgent()
    Runner(svc.store, agent, FakeTests()).execute(run_id)
    assert svc.store.get(run_id)["state"] == "waiting_approval"
    assert agent.calls == ["planner", "developer", "tester", "reviewer"]
    assert (repo / "main.py").read_text() == "developer local edits\n"
    svc.approve(run_id)
    assert svc.store.get(run_id)["state"] == "completed"


def test_fix_loop(service):
    svc, run_id, _ = service
    agent = FakeAgent(block=1)
    Runner(svc.store, agent, FakeTests()).execute(run_id)
    assert svc.store.get(run_id)["state"] == "waiting_approval"
    assert agent.calls[-3:] == ["fixer", "tester", "reviewer"]
    assert svc.store.get(run_id)["iteration"] == 1


def test_budget_exhaustion(service):
    svc, run_id, _ = service
    Runner(svc.store, FakeAgent(block=100), FakeTests()).execute(run_id)
    assert svc.store.get(run_id)["state"] == "needs_input"
    assert svc.store.get(run_id)["iteration"] == 3
    with pytest.raises(PipelineError, match="increase"):
        svc.resume(run_id)
    svc.resume(run_id, max_fixes=4)
    Runner(svc.store, FakeAgent(block=100), FakeTests()).execute(run_id)
    assert svc.store.get(run_id)["iteration"] == 4
    assert svc.store.get(run_id)["state"] == "needs_input"


def test_resume_preserves_completed_steps(service):
    svc, run_id, _ = service
    class Failing(FakeAgent):
        def run(self, req, *args):
            if req.role == "reviewer":
                raise PipelineError("temporary failure")
            return super().run(req, *args)
    Runner(svc.store, Failing(), FakeTests()).execute(run_id)
    assert svc.store.get(run_id)["state"] == "failed"
    svc.resume(run_id)
    agent = FakeAgent()
    Runner(svc.store, agent, FakeTests()).execute(run_id)
    assert agent.calls == ["reviewer"]
    assert svc.store.get(run_id)["state"] == "waiting_approval"


def test_approval_rejects_changed_head(service):
    svc, run_id, _ = service
    Runner(svc.store, FakeAgent(), FakeTests()).execute(run_id)
    worktree = svc.store.get(run_id)["worktree"]
    from pathlib import Path
    (Path(worktree) / "main.py").write_text("changed\n")
    with pytest.raises(PipelineError):
        svc.approve(run_id)


def test_cancellation(service):
    svc, run_id, _ = service
    svc.cancel(run_id)
    Runner(svc.store, FakeAgent(), FakeTests()).execute(run_id)
    assert svc.store.get(run_id)["state"] == "cancelled"
