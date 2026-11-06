"""Requirement-bound workspace edits and read-only historical proposals."""

from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service
from test_workspace import edit

from dev_orchestra.application.test_changes import TestChangeService as Changes
from dev_orchestra.application.test_changes import checksum, task_hash
from dev_orchestra.domain.models import AgentResult, PipelineError, ReviewResultV2, WorkspaceResult
from dev_orchestra.infrastructure.evidence import checkpoints
from dev_orchestra.infrastructure.files import digest, write
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
    def __init__(self, changes=True, role="developer"):
        super().__init__(block=int(role == "fixer"))
        self.changes = changes
        self.edit_role = role
        self.contexts = []

    def run(self, request, directory, stop):
        self.contexts.append(request.context)
        result = super().run(request, directory, stop)
        if request.role == "reviewer" and request.context.get("review_contract") == 2:
            return ReviewResultV2(**result.model_dump())
        return result

    def run_workspace(self, request, directory, session, stop):
        if request.role == self.edit_role:
            change = {
                "file": TEST_FILE,
                "criterion_ids": ["AC1"],
                "reason": "XML 활성화 요구사항에 따라 비활성 기대값을 활성으로 변경",
            }
            text = (session.root / TEST_FILE).read_text().replace(OLD_TEST, NEW_TEST)
            edit(session, TEST_FILE, text, test_change=change if self.changes else None)
            if request.role != "tester":
                edit(session, "main.py", "xml enabled\n")
                self.calls.append(request.role)
                return WorkspaceResult(summary="XML 활성화")
        return super().run_workspace(request, directory, session, stop)


def create_xml_run(svc, old, repo, prefix=""):
    (repo / "tests").mkdir(exist_ok=True)
    (repo / TEST_FILE).write_text(prefix + OLD_TEST)
    git = Git(repo)
    git.call("add", TEST_FILE)
    git.call("commit", "-m", "기존 XML 기대값 준비")
    return svc.create(repo, Path(svc.store.get(old)["directory"]) / "spec.md")


def legacy_pending(svc, old, repo, agent=None):
    """Persist historical data directly; no removed execution path is used."""
    rid = create_xml_run(svc, old, repo)
    run = svc.store.get(rid)
    Git(repo).create(Path(run["worktree"]), run["branch"], run["base_sha"])
    svc.store.update(
        rid,
        "needs_input",
        current_sha=run["base_sha"],
        next_role="developer",
        execution_contract="patch-v1",
        criteria=[{"id": "AC1", "description": "XML 활성화"}],
    )
    run = svc.store.get(rid)
    result = AgentResult(
        summary="XML 활성화",
        patch=PATCH,
        test_changes=[{"file": TEST_FILE, "criterion_ids": ["AC1"], "reason": "XML 활성화"}],
    )
    artifact = Path(run["directory"]) / "historical-result.json"
    proposal = {
        "id": "historical-proposal",
        "run_id": rid,
        "role": "developer",
        "iteration": 0,
        "revision": 0,
        "input_sha": run["current_sha"],
        "patch_hash": digest(PATCH.encode()),
        "result_hash": write(artifact, result.model_dump()),
        "artifact": artifact.name,
        "policy_hash": checksum(run["config"]["tests"]),
        "criteria_hash": checksum(run["criteria"]),
        "task_hash": task_hash(run),
        "changes": [c.model_dump() for c in result.test_changes],
        "created_at": run["created_at"],
    }
    proposal["proposal_hash"] = checksum(proposal)
    svc.store.update(rid, test_change_proposal=proposal)
    return svc, rid, repo, agent or XmlAgent()


@pytest.fixture
def pending(service):
    return legacy_pending(*service)


def test_requirement_test_changes_run_through_to_final_approval(service):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)
    agent = XmlAgent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls == ["planner", "developer", "tester", "reviewer"]
    assert len(run["test_change_records"]) == 1
    assert agent.contexts[-1]["test_change_records"][0]["changes"][0]["file"] == TEST_FILE
    assert (Path(run["worktree"]) / TEST_FILE).read_text() == NEW_TEST
    assert (repo / TEST_FILE).read_text() == OLD_TEST
    before, events = svc.store.get(rid), svc.store.events(rid)
    history = Changes(svc.store).view(rid)["history"]
    assert TEST_FILE in history[0]["patch"]
    assert svc.store.get(rid) == before and svc.store.events(rid) == events
    svc.approve(rid)


@pytest.mark.parametrize("role", ["developer", "tester", "fixer"])
def test_all_mutation_roles_keep_requirement_evidence(service, role):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo, prefix="const token = ++fileRequest.current;\n")
    Runner(svc.store, XmlAgent(role=role), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert run["test_change_records"][0]["role"] == role
    assert (Path(run["worktree"]) / TEST_FILE).read_text().endswith(NEW_TEST)
    checkpoints(svc.store, run)
    svc.approve(rid)


def test_missing_requirement_evidence_never_mutates_test(service):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)
    Runner(svc.store, XmlAgent(changes=False), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "failed"
    assert "requirement-bound" in run["error"]
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


def test_historical_proposal_can_be_read_but_not_approved_or_executed(pending):
    svc, rid, _, agent = pending
    before, events = svc.store.get(rid), svc.store.events(rid)
    assert Changes(svc.store).view(rid)["proposal"]["patch"] == PATCH
    assert svc.store.get(rid) == before and svc.store.events(rid) == events
    p = before["test_change_proposal"]
    with pytest.raises(PipelineError, match="read-only"):
        svc.decide_test_changes(rid, p["proposal_hash"], p["input_sha"], True)
    svc.resume(rid)
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "failed"
    assert not agent.calls
    assert (Path(before["worktree"]) / TEST_FILE).read_text() == OLD_TEST
