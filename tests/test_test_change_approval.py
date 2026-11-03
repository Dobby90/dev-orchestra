"""Requirement-driven test edits use disposable Git/SQLite and fake agents only."""

import json
from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service
from typer.testing import CliRunner

from dev_orchestra.agents.policy import apply_patch
from dev_orchestra.agents.prompts import prompt
from dev_orchestra.application.test_changes import TestChangeService as Changes
from dev_orchestra.cli import app
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


def create_xml_run(svc, old, repo):
    (repo / "tests").mkdir()
    (repo / TEST_FILE).write_text(OLD_TEST)
    git = Git(repo)
    git.call("add", TEST_FILE)
    git.call("commit", "-m", "기존 XML 기대값 준비")
    return svc.create(repo, Path(svc.store.get(old)["directory"]) / "spec.md")


@pytest.fixture
def pending(service):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)
    agent = XmlAgent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "needs_input", svc.store.get(rid).get("error")
    return svc, rid, repo, agent


def decide(svc, rid, approve=True, reason=""):
    p = svc.store.get(rid)["test_change_proposal"]
    svc.decide_test_changes(rid, p["proposal_hash"], p["input_sha"], approve, reason)


def test_full_patch_waits_for_exact_approval_and_reuses_saved_result(pending):
    svc, rid, repo, agent = pending
    before = svc.store.get(rid)
    tree = Path(before["worktree"])
    assert agent.calls == ["planner", "developer"]
    assert (tree / "main.py").read_text() == "original\n"
    assert (tree / TEST_FILE).read_text() == OLD_TEST
    view = Changes(svc.store).view(rid)
    assert view["proposal"]["patch"] == PATCH
    assert svc.store.get(rid) == before  # GET has no recovery or report side effects.
    with pytest.raises(PipelineError, match="approve or reject"):
        svc.resume_changes(before, "기존 테스트 변경을 허용합니다")
    decide(svc, rid)
    assert (tree / TEST_FILE).read_text() == OLD_TEST  # Approval only queues work.
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls == ["planner", "developer", "tester", "reviewer"]
    assert (tree / "main.py").read_text() == "xml enabled\n"
    assert (tree / TEST_FILE).read_text() == NEW_TEST
    assert not run.get("test_change_proposal")
    assert agent.contexts[-1]["approved_test_changes"][0]["changes"][0]["file"] == TEST_FILE
    assert (repo / "main.py").read_text() == "developer local edits\n"
    assert (repo / TEST_FILE).read_text() == OLD_TEST
    svc.approve(rid)
    assert svc.store.get(rid)["state"] == "completed"


@pytest.mark.parametrize("change", ["artifact", "head", "dirty", "revision", "policy", "criteria"])
def test_approval_rejects_stale_or_tampered_proposal(pending, change):
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
        svc.decide_test_changes(rid, proposal["proposal_hash"], proposal["input_sha"], True)
    assert not svc.store.get(rid).get("test_change_approvals")


def test_approved_result_is_checked_again_before_application(pending):
    svc, rid, _, agent = pending
    decide(svc, rid)
    run = svc.store.get(rid)
    with pytest.raises(PipelineError, match="before adding instructions"):
        svc.resume_changes(run, "추가 변경 지시")
    svc.store.update(rid, answers="추가 변경 지시")
    with pytest.raises(PipelineError, match="context"):
        Changes(svc.store).approved_result(svc.store.get(rid))
    svc.store.update(rid, answers=None)
    path = Path(run["directory"]) / run["test_change_proposal"]["artifact"]
    result = json.loads(path.read_text())
    result["patch"] = PATCH.replace("+xml enabled", "+different code")
    path.write_text(json.dumps(result))
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "failed"
    assert (Path(run["worktree"]) / TEST_FILE).read_text() == OLD_TEST
    assert agent.calls == ["planner", "developer"]
    assert Changes(svc.store).view(rid)["error"]
    decide(svc, rid, False, "손상된 제안을 폐기하고 다시 생성하세요")
    assert svc.store.get(rid)["test_change_proposal"] is None
    assert svc.store.get(rid)["test_change_approvals"] == []


def test_rejection_keeps_worktree_and_regenerates_with_instructions(pending):
    svc, rid, _, agent = pending
    with pytest.raises(PipelineError, match="instructions"):
        decide(svc, rid, False)
    svc.store.update(rid, worktree_removed=True)
    with pytest.raises(PipelineError, match="unavailable"):
        decide(svc, rid, False, "다시 작성하세요")
    svc.store.update(rid, worktree_removed=False)
    decide(svc, rid, False, "XML 기대값만 수정하고 회귀 검사는 보존하세요 token=CANARY")
    assert "CANARY" not in "".join(svc.store.get(rid)["directives"])
    assert svc.store.get(rid)["test_change_proposal"] is None
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "needs_input"
    assert agent.calls.count("developer") == 2
    assert "회귀 검사" in agent.contexts[-1]["directives"][-1]
    assert (Path(run["worktree"]) / TEST_FILE).read_text() == OLD_TEST


def test_stale_artifact_is_visible_for_rejection_without_authorizing_it(pending):
    svc, rid, _, _ = pending
    run = svc.store.get(rid)
    (Path(run["directory"]) / run["test_change_proposal"]["artifact"]).unlink()
    view = Changes(svc.store).view(rid)
    assert view["error"] and view["proposal"]["patch"] == ""
    with pytest.raises(OSError):
        decide(svc, rid)
    decide(svc, rid, False, "패치를 다시 생성하세요")
    assert svc.store.get(rid)["test_change_proposal"] is None


def test_paused_proposal_can_be_decided_without_generic_resume(pending):
    svc, rid, _, agent = pending
    svc.cancel(rid, pause=True)
    assert svc.store.get(rid)["state"] == "paused"
    decide(svc, rid)
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"


@pytest.mark.parametrize("role", ["tester", "fixer"])
def test_test_and_fix_roles_use_the_same_protected_test_approval(service, role):
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
    assert svc.store.get(rid)["state"] == "needs_input"
    assert svc.store.get(rid)["test_change_proposal"]["role"] == role
    calls = agent.calls.count(role)
    decide(svc, rid)
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls.count(role) == calls
    svc.approve(rid)


def test_no_proposal_for_missing_requirement_explanation(service):
    svc, old, repo = service
    rid = create_xml_run(svc, old, repo)
    Runner(svc.store, XmlAgent(changes=False), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "failed"
    assert "criterion IDs" in run["error"]
    assert not run.get("test_change_proposal")
    assert (Path(run["worktree"]) / TEST_FILE).read_text() == OLD_TEST


def test_approval_never_allows_deletion_or_tester_production_changes(pending):
    svc, rid, _, _ = pending
    tree = Path(svc.store.get(rid)["worktree"])
    for role, patch in [
        ("tester", PATCH),
        ("developer", f"--- a/{TEST_FILE}\n+++ /dev/null\n@@ -1 +0,0 @@\n-{OLD_TEST}"),
    ]:
        with pytest.raises(PipelineError):
            apply_patch(tree, patch, role, ["tests"], approved_patch_hash=digest(patch.encode()))
    assert (tree / TEST_FILE).read_text() == OLD_TEST


def test_final_approval_requires_retained_test_change_evidence(pending):
    svc, rid, _, agent = pending
    decide(svc, rid)
    Runner(svc.store, agent, FakeTests()).execute(rid)
    svc.store.update(rid, test_change_approvals=[])
    with pytest.raises(PipelineError, match="approval evidence"):
        checkpoints(svc.store, svc.store.get(rid))
    with pytest.raises(PipelineError):
        svc.approve(rid)


def test_cli_inspection_and_approval_use_the_shared_service(pending, monkeypatch):
    svc, rid, _, agent = pending
    monkeypatch.setattr("dev_orchestra.cli.RunService", lambda _: svc)
    monkeypatch.setattr(
        svc, "start", lambda *args, **kwargs: Runner(svc.store, agent, FakeTests()).execute(rid)
    )
    runner = CliRunner()
    response = runner.invoke(app, ["test-changes", rid])
    assert response.exit_code == 0, response.output
    proposal = json.loads(response.output)["proposal"]
    response = runner.invoke(
        app,
        [
            "approve-test-changes",
            rid,
            "--proposal-hash",
            proposal["proposal_hash"],
            "--input-sha",
            proposal["input_sha"],
        ],
    )
    assert response.exit_code == 0, response.output
    assert svc.store.get(rid)["state"] == "waiting_approval"


@pytest.mark.parametrize("role", ["planner", "developer", "tester", "fixer", "reviewer"])
def test_every_role_knows_requirement_changes_need_exact_patch_approval(role):
    text = prompt(
        AgentRequest(
            run_id="r", role=role, worktree="/tmp/example", iteration=0, input_sha="a", context={}
        )
    )
    assert "test_changes" in text
    assert "append-only" in text
    assert "exact-diff approval" in text
