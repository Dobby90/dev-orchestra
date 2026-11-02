"""Phase 5 evidence and shared commands use only disposable Git/SQLite."""

from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.application.commands import Commands
from dev_orchestra.application.results import Results
from dev_orchestra.domain.models import AgentResult, PipelineError, ReviewAssessment, ReviewResultV2
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.orchestration.runner import Runner


class Reviewer(FakeAgent):
    def __init__(self, block=0, omit=False):
        super().__init__(block)
        self.omit = omit

    def run(self, request, directory, stop):
        value = super().run(request, directory, stop)
        if request.role == "developer":
            return AgentResult(
                summary="change",
                patch="diff --git a/main.py b/main.py\n--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-original\n+changed\n",
            )
        if request.role == "reviewer":
            assessments = (
                []
                if self.omit
                else [
                    ReviewAssessment(
                        issue_id=i["issue_id"],
                        status="resolved",
                        candidate_sha=request.input_sha,
                        evidence="독립 재검증: current code and passing tests confirm the fix",
                    )
                    for i in request.context.get("open_issues", [])
                ]
            )
            return ReviewResultV2(**value.model_dump(), assessments=assessments)
        return value


@pytest.fixture
def completed(service):
    svc, old, repo = service
    upgrade(svc.store.path)
    rid = svc.create(repo, Path(svc.store.get(old)["directory"]) / "spec.md")
    agent = Reviewer()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval", svc.store.get(rid).get("error")
    return svc, rid, repo, agent


def test_request_changes_keeps_history_and_retests_noop_fix(completed):
    svc, rid, repo, agent = completed
    run = svc.store.get(rid)
    before = (repo / "main.py").read_bytes()
    sha = run["final_sha"]
    body = {
        "expected_sha": sha,
        "instructions": "추가 검증을 확인해 주세요",
        "new_max_fix_attempts": None,
    }
    command = Commands(svc)
    operation = command.execute(rid, "request-changes", run["version"], body, "same-change")
    assert operation["status"] == "applied"
    assert (
        command.execute(rid, "request-changes", run["version"], body, "same-change")["id"]
        == operation["id"]
    )
    with pytest.raises(PipelineError):
        command.execute(
            rid,
            "request-changes",
            run["version"],
            body | {"instructions": "different"},
            "same-change",
        )
    with svc.store.connection() as db:
        db.execute("UPDATE jobs SET state='finished' WHERE run_id=?", (rid,))
    Runner(svc.store, agent, FakeTests()).execute(rid)
    updated = svc.store.get(rid)
    assert updated["state"] == "waiting_approval", updated.get("error")
    assert updated["revision"] == 1 and updated["iteration"] == 1 and updated["final_sha"] == sha
    steps = svc.store.steps(rid)
    assert len([s for s in steps if s["role"] == "tester"]) == 2
    assert len([s for s in steps if s["role"] == "reviewer"]) == 2
    assert len([s for s in steps if s["role"] == "planner"]) == 1
    assert (repo / "main.py").read_bytes() == before
    assert Results(svc.store).changes(rid, sha, 0)["revision"] == 0


def test_snapshot_and_results_survive_cleanup_and_reject_tampering(completed):
    svc, rid, _, _ = completed
    results = Results(svc.store)
    run = svc.store.get(rid)
    view = results.changes(rid)
    assert view["available"] and len(view["files"]) == 1
    item = view["files"][0]
    assert "+changed" in results.diff(rid, item["file_id"], run["final_sha"], 0)["text"]
    svc.approve(rid)
    run = svc.store.get(rid)
    op = Commands(svc).execute(rid, "cleanup", run["version"], {"confirm": True}, "cleanup")
    assert op["status"] == "applied" and not Path(run["worktree"]).exists()
    assert "+changed" in results.diff(rid, item["file_id"], run["final_sha"], 0)["text"]
    assert results.tests(rid)["status"] == "passed"
    path, _ = results.artifact(rid, item["artifact_id"])
    path.write_text("tampered")
    with pytest.raises(PipelineError, match="modified"):
        results.diff(rid, item["file_id"], run["final_sha"], 0)
    with pytest.raises(PipelineError):
        results.artifact(rid, "artifact-other")


def test_omitted_prior_issue_cannot_become_fixed(service):
    svc, old, repo = service
    upgrade(svc.store.path)
    rid = svc.create(repo, Path(svc.store.get(old)["directory"]) / "spec.md", max_fixes=1)
    Runner(svc.store, Reviewer(block=1, omit=True), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "needs_input" and run["error"] == "fix budget exhausted"
    review = Results(svc.store).reviews(rid)
    assert len(review["issues"]) == 1 and review["issues"][0]["status"] == "unresolved"
    with pytest.raises(PipelineError):
        svc.approve(rid)


def test_explicit_assessment_bound_to_current_test_sha(service):
    svc, old, repo = service
    upgrade(svc.store.path)
    rid = svc.create(repo, Path(svc.store.get(old)["directory"]) / "spec.md", max_fixes=1)
    Runner(svc.store, Reviewer(block=1), FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval", svc.store.get(rid).get("error")
    issue = Results(svc.store).reviews(rid)["issues"][0]
    assert issue["status"] == "resolved" and issue["resolution_evidence"]
    assert issue["assessed_sha"] == svc.store.get(rid)["final_sha"]


def test_revision_budget_and_defer_are_persistent(completed):
    svc, rid, _, _ = completed
    command = Commands(svc)
    run = svc.store.get(rid)
    op = command.execute(rid, "defer", run["version"], {"reason": "나중에 확인"}, "defer")
    assert svc.store.get(rid)["state"] == "waiting_approval" and op["status"] == "applied"
    assert (
        command.execute(rid, "defer", run["version"], {"reason": "나중에 확인"}, "defer")["id"]
        == op["id"]
    )
    with pytest.raises(PipelineError):
        command.execute(rid, "approve", run["version"], {"expected_sha": run["final_sha"]}, "stale")
    run = svc.store.get(rid)
    cfg = run["config"]
    cfg["workflow"]["max_fix_attempts"] = 0
    svc.store.update(rid, config=cfg)
    run = svc.store.get(rid)
    with pytest.raises(PipelineError):
        command.execute(
            rid,
            "request-changes",
            run["version"],
            {
                "expected_sha": run["final_sha"],
                "instructions": "수정",
                "new_max_fix_attempts": None,
            },
            "budget",
        )
    with svc.store.connection() as db:
        assert (
            db.execute("SELECT count(*) FROM run_directives WHERE run_id=?", (rid,)).fetchone()[0]
            == 1
        )
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_approve_and_changes_same_version_have_one_winner(completed):
    from concurrent.futures import ThreadPoolExecutor

    svc, rid, _, _ = completed
    run = svc.store.get(rid)

    def perform(kind):
        try:
            return Commands(svc).execute(
                rid,
                kind,
                run["version"],
                {
                    "expected_sha": run["final_sha"],
                    "instructions": "확인",
                    "new_max_fix_attempts": None,
                },
                kind,
            )
        except PipelineError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(perform, ["approve", "request-changes"]))
    assert sum(value is not None for value in values) == 1
    assert svc.store.get(rid)["state"] in {"completed", "queued"}
    with svc.store.connection() as db:
        assert (
            db.execute("SELECT count(*) FROM operations WHERE run_id=?", (rid,)).fetchone()[0] == 1
        )


def test_report_version_is_immutable_under_competing_writers(completed):
    from concurrent.futures import ThreadPoolExecutor

    svc, rid, _, _ = completed
    results = Results(svc.store)
    version = svc.store.get(rid)["version"] + 100
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda text: results.save_report(rid, text, version), ["first", "second"]))
    with svc.store.connection() as db:
        aid = db.execute(
            "SELECT artifact_id FROM reports WHERE run_id=? AND version=?", (rid, version)
        ).fetchone()[0]
    path, _ = results.artifact(rid, aid)
    assert path.read_text() in {"first", "second"}
    before = path.read_bytes()
    results.save_report(rid, "third", version)
    assert path.read_bytes() == before


def test_legacy_missing_test_counts_remain_unknown(monkeypatch):
    results = Results(None)
    monkeypatch.setattr(
        results,
        "selected",
        lambda *args: ({}, {"id": 1, "output_sha": "a", "status": "completed"}, []),
    )
    monkeypatch.setattr(
        results,
        "payload",
        lambda *args: {"execution": {"passed": True, "commands": [{"exit_code": 0, "total": 1}]}},
    )
    view = results.tests("legacy")
    assert view["status"] == "passed" and view["total"] == 1
    assert view["passed"] is None and view["failed"] is None and view["skipped"] is None
    assert view["legacy_incomplete"]


def test_cleanup_after_worktree_creation_before_sha_checkpoint(service):
    svc, rid, repo = service
    run = svc.store.get(rid)
    Git(repo).create(Path(run["worktree"]), run["branch"], run["base_sha"])
    svc.store.update(rid, "failed", error="interrupted before SHA checkpoint")
    svc.cleanup(rid)
    assert not Path(run["worktree"]).exists()
    assert Git(repo).baseline(run["branch"]) == run["base_sha"]
    assert (repo / "main.py").read_text() == "developer local edits\n"
