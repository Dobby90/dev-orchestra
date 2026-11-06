"""Phase 5 evidence and shared commands use only disposable Git/SQLite."""

from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.application.commands import Commands
from dev_orchestra.application.results import Results
from dev_orchestra.domain.models import PipelineError, ReviewAssessment, ReviewResultV2
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.orchestration.runner import Runner


class ReportTests(FakeTests):
    def run(self, worktree, config, directory, stop=lambda: None):
        from dev_orchestra.infrastructure.files import digest
        from dev_orchestra.infrastructure.test_runner import junit_bundle

        result = super().run(worktree, config, directory, stop)
        report = worktree / config.commands["unit"].junit_report
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text('<testsuite><testcase name="unit" /></testsuite>')
        retained = directory / "unit.junit.xml"
        retained.parent.mkdir(parents=True, exist_ok=True)
        retained.write_text(junit_bundle([report]))
        result["commands"][0].update(
            junit_path=str(retained), junit_hash=digest(retained.read_bytes())
        )
        return result


@pytest.fixture
def completed_reports(service):
    svc, old, repo = service
    upgrade(svc.store.path)
    rid = svc.create(repo, Path(svc.store.get(old)["directory"]) / "spec.md")
    Runner(svc.store, Reviewer(), ReportTests()).execute(rid)
    svc.approve(rid)
    return svc, rid, repo


def test_cleanup_preserves_verified_report_and_branch(completed_reports):
    svc, rid, repo = completed_reports
    run = svc.store.get(rid)
    report = Path(run["worktree"]) / "build/results.xml"
    original = report.read_bytes()
    svc.cleanup(rid)
    assert not Path(run["worktree"]).exists()
    archived = list(Path(run["directory"]).glob("cleanup-reports-*/build/results.xml"))
    assert len(archived) == 1 and archived[0].read_bytes() == original
    assert Git(repo).sha() == run["base_sha"]
    assert Git(repo).call("rev-parse", run["branch"]) == run["final_sha"]


@pytest.mark.parametrize("change", ["unknown", "report", "tracked", "symlink", "retained"])
def test_cleanup_failure_is_persistent_and_preserves_files(completed_reports, change):
    svc, rid, _ = completed_reports
    run = svc.store.get(rid)
    worktree = Path(run["worktree"])
    report = worktree / "build/results.xml"
    if change == "unknown":
        (worktree / "build/personal.txt").write_text("preserve")
    elif change == "report":
        report.write_text('<testsuite><testcase name="changed" /></testsuite>')
    elif change == "tracked":
        (worktree / "main.py").write_text("uncommitted")
    elif change == "symlink":
        report.unlink()
        report.symlink_to(worktree / "main.py")
    else:
        aid = run["test_result"]["execution"]["commands"][0]["junit_artifact_id"]
        retained, _ = Results(svc.store).artifact(rid, aid)
        retained.write_text("tampered")
    before = report.read_bytes()
    op = Commands(svc).execute(rid, "cleanup", run["version"], {"confirm": True})
    assert op["status"] == "needs_input" and op["error"]
    assert worktree.exists() and report.read_bytes() == before
    assert not list(Path(run["directory"]).glob("cleanup-reports-*"))
    # Fresh queries retain the error even without the browser's last operation ID.
    snapshot = svc.queries.snapshot(rid)
    assert snapshot["execution"]["cleanup_status"] == "needs_input"
    assert snapshot["execution"]["cleanup_error"] == op["error"]
    assert not snapshot["execution"]["worktree_removed"]


class Reviewer(FakeAgent):
    def __init__(self, block=0, omit=False):
        super().__init__(block)
        self.omit = omit

    def run(self, request, directory, stop):
        value = super().run(request, directory, stop)
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
            return ReviewResultV2(
                **value.model_dump(exclude={"assessments"}), assessments=assessments
            )
        return value

    def run_workspace(self, request, directory, session, stop):
        if request.role == "developer":
            from test_workspace import edit

            from dev_orchestra.domain.models import WorkspaceResult

            edit(session, "main.py", "changed\n")
            self.calls.append(request.role)
            return WorkspaceResult(summary="기능 변경")
        return super().run_workspace(request, directory, session, stop)


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


@pytest.mark.parametrize("transport", ["cli", "api"])
def test_cleanup_records_removal_and_hides_resume(completed, transport):
    svc, rid, repo, _ = completed
    svc.cancel(rid, pause=True)
    svc.store.update(rid, "needs_input", project_id="display-only")
    run = svc.store.get(rid)
    branch = Git(repo).call("rev-parse", run["branch"])
    if transport == "api":
        op = Commands(svc).execute(rid, "cleanup", run["version"], {"confirm": True})
        assert op["status"] == "applied"
    else:
        svc.cleanup(rid)
    assert not Path(run["worktree"]).exists()
    assert Git(repo).call("rev-parse", run["branch"]) == branch
    snapshot = svc.queries.snapshot(rid)
    assert snapshot["execution"]["worktree_removed"]
    assert "resume" not in snapshot["allowed_actions"]
    with pytest.raises(PipelineError, match="create a new Run"):
        svc.resume(rid)
    with pytest.raises(PipelineError, match="create a new Run"):
        Commands(svc).execute(rid, "resume", svc.store.get(rid)["version"], {})
    version = svc.store.get(rid)["version"]
    svc.cleanup(rid, expected_version=version)
    assert svc.store.get(rid)["version"] == version


@pytest.mark.parametrize("transport", ["cli", "api"])
def test_cleanup_retries_removal_before_state_commit(completed, monkeypatch, transport):
    svc, rid, _, _ = completed
    svc.approve(rid)
    run = svc.store.get(rid)
    update = svc.store.update

    def fail_after_removal(*args, **kwargs):
        if kwargs.get("worktree_removed"):
            raise RuntimeError("interrupted after removal")
        return update(*args, **kwargs)

    monkeypatch.setattr(svc.store, "update", fail_after_removal)
    commands = Commands(svc)
    with pytest.raises(RuntimeError, match="interrupted"):
        if transport == "api":
            commands.execute(rid, "cleanup", run["version"], {"confirm": True}, "retry")
        else:
            svc.cleanup(rid)
    assert not Path(run["worktree"]).exists()
    monkeypatch.setattr(svc.store, "update", update)
    if transport == "api":
        op = commands.execute(rid, "cleanup", run["version"], {"confirm": True}, "retry")
        assert op["status"] == "applied"
        version = svc.store.get(rid)["version"]
        commands.finish_cleanup(op["id"], rid, version - 1)
        assert svc.store.get(rid)["version"] == version
        assert commands.get(op["id"])["status"] == "applied"
    else:
        svc.cleanup(rid)
    assert svc.store.get(rid)["worktree_removed"]


def test_cleanup_preserves_dirty_worktree(completed):
    svc, rid, _, _ = completed
    svc.approve(rid)
    worktree = Path(svc.store.get(rid)["worktree"])
    (worktree / "main.py").write_text("사용자 변경\n")
    with pytest.raises(PipelineError):
        svc.cleanup(rid)
    assert (worktree / "main.py").read_text() == "사용자 변경\n"
    assert not svc.store.get(rid).get("worktree_removed")


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
