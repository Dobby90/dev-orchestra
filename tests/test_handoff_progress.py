"""Workspace commits and recovery presentation with real host-owned evidence."""

from test_workflow import FakeTests
from test_workspace import checkpoint, edit
from test_workspace import setup as setup
from test_workspace_recovery import Agent, planned

from dev_orchestra.domain.models import StopRequested
from dev_orchestra.infrastructure.migrations import upgrade


def test_workspace_progress_uses_host_snapshots_and_preserved_completed_results(setup):
    svc, rid, _ = setup
    upgrade(svc.store.path)

    class Interrupted(Agent):
        def run_workspace(self, req, directory, session, stop):
            if req.role == "developer" and not session.data["commits"]:
                edit(session, "main.py", "first\n")
                checkpoint(session, "one", ["main.py"])
                edit(session, "pending.py", "preserved\n")
                raise StopRequested("pause")
            return super().run_workspace(req, directory, session, stop)

    runner = planned(setup, Interrupted(), FakeTests())
    runner.execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "paused"
    original = svc.store.steps(rid)[-1]
    version = run["version"]
    view = svc.queries.snapshot(rid)
    progress = view["workspace_progress"]
    assert progress["recovery"] == "same_role"
    assert len(progress["commits"]) == 1
    assert progress["checkpoint_sha"] == run["current_sha"]
    assert progress["preserved_changes"] == 2
    step = svc.queries.steps(rid)[-1]
    assert step["commits"] == progress["commits"]
    assert svc.store.get(rid)["version"] == version
    svc.resume(rid)
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"
    step = next(s for s in svc.queries.steps(rid) if s["role"] == "developer")
    assert step["id"] == original["id"]
    assert len(step["commits"]) == 2
    svc.approve(rid)
    svc.cleanup(rid)
    assert (
        next(s for s in svc.queries.steps(rid) if s["role"] == "developer")["commits"]
        == step["commits"]
    )
    assert svc.queries.snapshot(rid)["handoff"]["status"] == "matching"


def test_metadata_repair_view_does_not_claim_files_need_reimplementation(setup):
    from dev_orchestra.agents.contracts import ResponseError
    from dev_orchestra.domain.models import WorkspaceResult

    svc, rid, _ = setup
    upgrade(svc.store.path)

    class Repair(Agent):
        def run_workspace(self, req, directory, session, stop):
            if req.role == "developer":
                if not session.data["commits"]:
                    edit(session, "main.py", "completed code\n")
                    checkpoint(session, "one", ["main.py"])
                    raise ResponseError("summary invalid")
                assert session.data["metadata_only"]
                assert svc.queries.snapshot(rid)["workspace_progress"]["recovery"] == "metadata"
                return WorkspaceResult(summary="저장된 구현의 결과 요약 보완")
            return super().run_workspace(req, directory, session, stop)

    runner = planned(setup, Repair(), FakeTests())
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"


def test_approval_and_cleanup_leave_parallel_worker_and_branch_untouched(setup):
    from pathlib import Path

    from test_parallel_execution import CTX, run_worker

    from dev_orchestra.infrastructure.coordination import configure_capacity
    from dev_orchestra.infrastructure.git import Git

    svc, rid, repo = setup
    runner = planned(setup, Agent(), FakeTests())
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"
    spec = Path(svc.store.get(rid)["directory"]) / "spec.md"
    other = svc.create(repo, spec)
    other_runner = planned((svc, other, repo), Agent(), FakeTests())
    del other_runner
    configure_capacity(svc.store.path, total=2, repo=repo, repository=2)
    entered, release = CTX.Queue(), CTX.Event()
    process = CTX.Process(target=run_worker, args=(str(svc.store.path), other, entered, release))
    process.start()
    try:
        assert entered.get(timeout=30) == other
        run = svc.store.get(other)
        sha = Git(Path(run["worktree"])).sha()
        before = (Path(run["worktree"]) / (other + ".py")).read_bytes()
        svc.approve(rid)
        svc.cleanup(rid)
        assert process.is_alive()
        assert Git(repo).baseline(run["branch"]) == sha
        assert Git(Path(run["worktree"])).sha() == sha
        assert (Path(run["worktree"]) / (other + ".py")).read_bytes() == before
    finally:
        release.set()
        process.join(120)
        if process.is_alive():
            process.terminate()
            process.join(10)
    assert process.exitcode == 0
    assert svc.store.get(other)["state"] == "waiting_approval"
