"""Requirement-driven test edits use disposable Git/SQLite and fake agents only."""

from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.agents.policy import apply_patch
from dev_orchestra.agents.prompts import prompt
from dev_orchestra.application.test_changes import TestChangeService as Changes
from dev_orchestra.domain.models import (
    AgentRequest,
    AgentResult,
    PipelineError,
    ReviewResultV2,
)
from dev_orchestra.domain.models import (
    TestChange as Change,
)
from dev_orchestra.infrastructure.evidence import checkpoints
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.orchestration.runner import Runner

TEST_FILE = "tests/test_tabs.py"
OLD_TEST = "assert xml_enabled is False\n"
NEW_TEST = "assert xml_enabled is True\n"
PATCH = (
    "diff --git a/main.py b/main.py\n--- a/main.py\n+++ b/main.py\n"
    "@@ -1 +1 @@\n-original\n+xml enabled\n"
    "diff --git a/tests/test_tabs.py b/tests/test_tabs.py\n"
    "--- a/tests/test_tabs.py\n+++ b/tests/test_tabs.py\n"
    "@@ -1 +1 @@\n-assert xml_enabled is False\n+assert xml_enabled is True\n"
)


class XmlAgent(FakeAgent):
    def __init__(self, patch=PATCH, changes=True):
        super().__init__()
        self.patch = patch
        self.changes = changes
        self.contexts = []

    def run(self, request, directory, stop):
        self.contexts.append(request.context)
        value = super().run(request, directory, stop)
        if request.role == "developer":
            return AgentResult(
                summary="XML 활성화",
                patch=self.patch,
                test_changes=[
                    Change(
                        file=TEST_FILE,
                        criterion_ids=["AC1"],
                        reason="XML 활성화 요구사항에 따라 비활성 기대값을 활성으로 변경",
                    )
                ]
                if self.changes
                else [],
            )
        if request.role == "reviewer" and request.context.get("review_contract") == 2:
            return ReviewResultV2(**value.model_dump())
        return value


def create_xml_run(svc, old, repo, prefix=""):
    (repo / "tests").mkdir()
    (repo / TEST_FILE).write_text(prefix + OLD_TEST)
    git = Git(repo)
    git.call("add", TEST_FILE)
    git.call("commit", "-m", "기존 XML 기대값 준비")
    return svc.create(repo, Path(svc.store.get(old)["directory"]) / "spec.md")


def legacy_pending(svc, old, repo, agent=None):
    """Create a proposal in the format persisted before automatic test changes."""
    rid = create_xml_run(svc, old, repo)
    agent = agent or XmlAgent()
    captured = []

    class Capture:
        def run(self, request, *args):
            result = agent.run(request, *args)
            if request.role == "developer":
                captured.append(result)
                return result.model_copy(update={"questions": ["legacy test-change gate"]})
            return result

    Runner(svc.store, Capture(), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    Changes(svc.store).propose(run, captured[0], [TEST_FILE], Path(run["directory"]) / "legacy")
    return svc, rid, repo, agent


@pytest.fixture
def pending(service):
    return legacy_pending(*service)


def decide(svc, rid, approve=True, reason=""):
    p = svc.store.get(rid)["test_change_proposal"]
    svc.decide_test_changes(rid, p["proposal_hash"], p["input_sha"], approve, reason)


def test_requirement_test_changes_run_through_to_final_approval(service):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)
    agent = XmlAgent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls == ["planner", "developer", "tester", "reviewer"]
    assert not run.get("test_change_proposal")
    assert not run.get("test_change_approvals")
    assert len(run["test_change_records"]) == 1
    assert agent.contexts[-1]["test_change_records"][0]["changes"][0]["file"] == TEST_FILE
    tree = Path(run["worktree"])
    assert (tree / "main.py").read_text() == "xml enabled\n"
    assert (tree / TEST_FILE).read_text() == NEW_TEST
    assert (repo / "main.py").read_text() == "developer local edits\n"
    assert (repo / TEST_FILE).read_text() == OLD_TEST
    assert not any(e["kind"] == "test_changes.proposed" for e in svc.store.events(rid))
    before, events = svc.store.get(rid), svc.store.events(rid)
    history = Changes(svc.store).view(rid)["history"]
    assert history[0]["patch"] == PATCH
    assert history[0]["changes"][0]["file"] == TEST_FILE
    assert svc.store.get(rid) == before and svc.store.events(rid) == events
    report = (Path(run["directory"]) / "final-report.md").read_text()
    assert TEST_FILE in report and "XML 활성화 요구사항" in report
    svc.approve(rid)
    assert svc.store.get(rid)["state"] == "completed"


def test_protected_test_patch_preserves_request_counter_context(service):
    svc, old, repo = service
    counter = "const token = ++fileRequest.current, inputVersion = version.current;\n"
    rid = create_xml_run(svc, old, repo, prefix=counter)
    patch = PATCH.replace("@@ -1 +1 @@\n-assert", "@@ -1,2 +1,2 @@\n " + counter + "-assert")
    Runner(svc.store, XmlAgent(patch=patch), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert (Path(run["worktree"]) / TEST_FILE).read_text() == counter + NEW_TEST
    assert (repo / TEST_FILE).read_text() == counter + OLD_TEST
    assert Changes(svc.store).view(rid)["history"][0]["patch"] == patch
    checkpoints(svc.store, run)
    svc.approve(rid)
    assert svc.store.get(rid)["state"] == "completed"


def test_protected_test_patch_with_secret_is_rejected_before_application(service):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)
    patch = PATCH.replace("+xml enabled", '+const token = "CANARY";')
    Runner(svc.store, XmlAgent(patch=patch), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "failed"
    assert run["error"] == "protected test patch contains redacted data"
    assert not run.get("test_change_records")
    assert (Path(run["worktree"]) / TEST_FILE).read_text() == OLD_TEST
    assert (Path(run["worktree"]) / "main.py").read_text() == "original\n"


@pytest.mark.parametrize("approved", [False, True])
def test_legacy_proposal_resumes_saved_result_without_intermediate_approval(pending, approved):
    svc, rid, _, agent = pending
    if approved:
        decide(svc, rid)
    else:
        svc.resume(rid)
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls == ["planner", "developer", "tester", "reviewer"]
    assert not run.get("test_change_proposal")
    assert not run.get("test_change_application")
    svc.approve(rid)


@pytest.mark.parametrize("change", ["artifact", "head", "dirty", "revision", "policy", "criteria"])
def test_legacy_approval_rejects_stale_or_tampered_proposal(pending, change):
    svc, rid, _, _ = pending
    run = svc.store.get(rid)
    proposal = run["test_change_proposal"]
    if change == "artifact":
        (Path(run["directory"]) / proposal["artifact"]).write_text("tampered")
    elif change in {"dirty", "head"}:
        tree = Path(run["worktree"])
        (tree / "main.py").write_text("external\n")
        if change == "head":
            Git(tree).commit("외부 변경 재현")
    elif change == "revision":
        svc.store.update(rid, revision=1)
    elif change == "criteria":
        svc.store.update(rid, criteria=[{"id": "AC1", "description": "changed"}])
    else:
        config = run["config"]
        config["tests"]["write_paths"].append("other-tests")
        svc.store.update(rid, config=config)
    with pytest.raises(PipelineError):
        decide(svc, rid)
    assert not svc.store.get(rid).get("test_change_approvals")


@pytest.mark.parametrize("change", ["artifact", "revision", "criteria", "answers"])
def test_legacy_automatic_resume_checks_saved_context(pending, change):
    svc, rid, _, agent = pending
    run = svc.store.get(rid)
    if change == "artifact":
        (Path(run["directory"]) / run["test_change_proposal"]["artifact"]).write_text("tampered")
    elif change == "criteria":
        svc.store.update(rid, criteria=[{"id": "AC1", "description": "changed"}])
    else:
        svc.store.update(rid, **{change: 1 if change == "revision" else "changed"})
    svc.resume(rid)
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "failed"
    assert (Path(run["worktree"]) / TEST_FILE).read_text() == OLD_TEST
    decide(svc, rid, False, "손상된 제안을 폐기하고 다시 생성하세요")
    assert svc.store.get(rid)["test_change_proposal"] is None


@pytest.mark.parametrize("role", ["tester", "fixer"])
def test_all_mutation_roles_change_tests_automatically(service, role):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)

    class RoleAgent(FakeAgent):
        def __init__(self):
            super().__init__(block=1 if role == "fixer" else 0)

        def run(self, request, directory, stop):
            result = super().run(request, directory, stop)
            if request.role == role:
                result.patch = PATCH[PATCH.index("diff --git a/tests/") :]
                result.test_changes = [
                    Change(file=TEST_FILE, criterion_ids=["AC1"], reason="XML 활성화 기대값 변경")
                ]
            return result

    agent = RoleAgent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert run["test_change_records"][0]["role"] == role
    svc.approve(rid)


@pytest.mark.parametrize("invalid", ["missing", "criterion", "reason"])
def test_missing_requirement_evidence_never_mutates_worktree(service, invalid):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)

    class InvalidAgent(XmlAgent):
        def run(self, request, *args):
            result = super().run(request, *args)
            if request.role == "developer":
                if invalid == "missing":
                    result.test_changes = []
                elif invalid == "criterion":
                    result.test_changes[0].criterion_ids = ["unknown"]
                else:
                    result.test_changes[0].reason = " "
            return result

    Runner(svc.store, InvalidAgent(), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "needs_input"
    assert "response repair budget exhausted" in run["error"]
    attempts = [step for step in svc.store.steps(rid) if step["role"] == "developer"]
    assert len(attempts) == 2 and all(step["status"] == "failed" for step in attempts)
    assert "criterion IDs" in run["error"]
    assert not run.get("test_change_proposal")
    assert (Path(run["worktree"]) / TEST_FILE).read_text() == OLD_TEST
    assert (Path(run["worktree"]) / "main.py").read_text() == "original\n"


@pytest.mark.parametrize("invalid", ["commands", "coverage"])
def test_tester_contract_is_checked_before_applying_patch(service, invalid):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)

    class InvalidTester(FakeAgent):
        def run(self, request, *args):
            result = super().run(request, *args)
            if request.role == "tester":
                result.patch = PATCH[PATCH.index("diff --git a/tests/") :]
                result.test_changes = [
                    Change(file=TEST_FILE, criterion_ids=["AC1"], reason="XML 활성화")
                ]
                if invalid == "commands":
                    result.command_ids = []
                else:
                    result.coverage = []
            return result

    Runner(svc.store, InvalidTester(), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "needs_input"
    assert "response repair budget exhausted" in run["error"]
    attempts = [step for step in svc.store.steps(rid) if step["role"] == "tester"]
    assert len(attempts) == 2 and all(step["status"] == "failed" for step in attempts)
    assert (Path(run["worktree"]) / TEST_FILE).read_text() == OLD_TEST
    assert not run.get("test_change_records")


def test_test_change_evidence_is_required_for_final_approval(service):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)
    Runner(svc.store, XmlAgent(), FakeTests()).execute(rid)
    svc.store.update(rid, test_change_records=[])
    with pytest.raises(PipelineError, match="change evidence"):
        checkpoints(svc.store, svc.store.get(rid))
    with pytest.raises(PipelineError):
        svc.approve(rid)
    assert Changes(svc.store).view(rid)["error"]


def test_automatic_changes_never_allow_deletion_or_tester_production_changes(pending):
    svc, rid, _, _ = pending
    tree = Path(svc.store.get(rid)["worktree"])
    for role, patch in [
        ("tester", PATCH),
        ("developer", f"--- a/{TEST_FILE}\n+++ /dev/null\n@@ -1 +0,0 @@\n-{OLD_TEST}"),
    ]:
        with pytest.raises(PipelineError):
            apply_patch(tree, patch, role, ["tests"], validated_patch_hash=digest(patch.encode()))
    assert (tree / TEST_FILE).read_text() == OLD_TEST


@pytest.mark.parametrize("role", ["planner", "developer", "tester", "fixer", "reviewer"])
def test_every_role_knows_test_changes_are_automatic_and_requirement_bound(role):
    text = prompt(
        AgentRequest(
            run_id="r", role=role, worktree="/tmp/example", iteration=0, input_sha="a", context={}
        )
    )
    assert "test_changes" in text
    assert "append-only" in text
    assert "Do not ask for intermediate test-change approval" in text
    assert "final verified candidate" in text


@pytest.mark.parametrize("interruption", ["pause", "failure", "tampered"])
def test_applied_test_patch_resumes_without_regenerating_or_reapplying(service, interruption):
    from dev_orchestra.domain.models import StopRequested

    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)

    class Tester(FakeAgent):
        def run(self, request, *args):
            result = super().run(request, *args)
            if request.role == "tester":
                result.patch = PATCH[PATCH.index("diff --git a/tests/") :]
                result.test_changes = [
                    Change(file=TEST_FILE, criterion_ids=["AC1"], reason="XML 활성화")
                ]
            return result

    class InterruptingTests(FakeTests):
        def run(self, *args):
            if interruption in {"pause", "tampered"}:
                raise StopRequested("pause")
            raise PipelineError("temporary validation failure")

    agent = Tester()
    Runner(svc.store, agent, InterruptingTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == ("failed" if interruption == "failure" else "paused")
    assert (Path(run["worktree"]) / TEST_FILE).read_text() == NEW_TEST
    sha = run["current_sha"]
    assert run["test_change_application"] and not run.get("test_change_proposal")
    view = Changes(svc.store).view(rid)
    assert view["error"] is None
    assert view["history"][0]["patch"] == PATCH[PATCH.index("diff --git a/tests/") :]
    if interruption == "tampered":
        artifact = Path(run["directory"]) / run["test_change_application"]["artifact"]
        artifact.write_text("tampered")
        with pytest.raises(PipelineError, match="applied test change result checksum mismatch"):
            svc.resume(rid)
        assert Changes(svc.store).view(rid)["error"]
        assert (Path(run["worktree"]) / TEST_FILE).read_text() == NEW_TEST
        return
    svc.resume(rid)
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert run["current_sha"] == sha
    assert agent.calls == ["planner", "developer", "tester", "reviewer"]
    assert len(run["test_change_records"]) == 1
    assert not run.get("test_change_application")
    checkpoints(svc.store, run)
    svc.approve(rid)


def test_test_failures_still_enter_automatic_fix_and_revalidation(service):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)

    class FailingOnce(FakeTests):
        calls = 0

        def run(self, *args):
            self.calls += 1
            return {
                "passed": self.calls > 1,
                "commands": [{"exit_code": int(self.calls == 1), "total": 1}],
            }

    agent, tests = XmlAgent(), FailingOnce()
    Runner(svc.store, agent, tests).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls == [
        "planner",
        "developer",
        "tester",
        "reviewer",
        "fixer",
        "tester",
        "reviewer",
    ]
    assert tests.calls == 2
    assert not run.get("test_change_approvals")
    svc.approve(rid)
