"""Local-only approval/handoff and preservation; never calls a model."""

from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.application.handoff import handoff, verify_branch
from dev_orchestra.application.results import Results
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.orchestration.runner import Runner


def ready(service, web=False):
    svc, rid, repo = service
    if web:
        upgrade(svc.store.path)
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"
    return svc, rid, repo


@pytest.mark.parametrize("web", [False, True])
def test_local_handoff_cleanup_preserves_branch_history_and_original(service, tmp_path, web):
    service[0].store.update(service[1], branch="custom/local-handoff")
    svc, rid, repo = ready(service, web)
    original = Git(repo)
    before = original.sha(), original.call("branch", "--show-current"), original.dirty()
    assert original.call("remote") == ""
    svc.approve(rid)
    run = svc.store.get(rid)
    value = handoff(run)
    assert value["status"] == "matching"
    assert value["approved_sha"] == original.baseline(run["branch"])
    assert value["branch"] == "custom/local-handoff"
    assert all(str(repo) in cmd for cmd in value["commands"])
    report = Path(run["directory"]) / "final-report.md"
    assert "로컬 브랜치 인계" in report.read_text()
    assert run["final_sha"] in report.read_text()
    steps = svc.store.steps(rid)
    if web:
        saved_report = Results(svc.store).report(rid)
        tests = Results(svc.store).tests(rid)
    svc.cleanup(rid)
    assert not Path(run["worktree"]).exists()
    assert original.baseline(run["branch"]) == run["final_sha"]
    assert svc.store.steps(rid) == steps
    assert report.is_file()
    assert handoff(svc.store.get(rid))["status"] == "matching"
    if web:
        assert Results(svc.store).report(rid)["markdown"] == saved_report["markdown"]
        assert Results(svc.store).tests(rid) == tests
    checkout = tmp_path / "inspection"
    original.call("worktree", "add", str(checkout), run["branch"])
    assert Git(checkout).sha() == run["final_sha"]
    assert before == (original.sha(), original.call("branch", "--show-current"), original.dirty())
    assert (repo / "main.py").read_text() == "developer local edits\n"
    with pytest.raises(PipelineError, match="removed|cleaned"):
        svc.resume(rid)


@pytest.mark.parametrize("tamper", ["deleted", "moved", "detached", "other_repository", "identity"])
def test_approval_rejects_branch_or_repository_tampering_preserving_files(
    service, tmp_path, tamper
):
    svc, rid, repo = ready(service)
    run = svc.store.get(rid)
    git = Git(Path(run["worktree"]))
    original = Git(repo)
    # Create another commit so branch movement cannot accidentally be a no-op.
    (Path(run["worktree"]) / "new.py").write_text("value = 1\n")
    sha = git.commit("검사 후보")
    svc.store.update(rid, final_sha=sha, current_sha=sha)
    if tamper == "deleted":
        original.call("update-ref", "-d", "refs/heads/" + run["branch"])
    elif tamper == "moved":
        original.call("update-ref", "refs/heads/" + run["branch"], run["base_sha"])
    elif tamper == "detached":
        git.call("checkout", "--detach", sha)
    elif tamper == "identity":
        svc.store.update(rid, repository_identity={"common": "unexpected"})
    else:
        other = tmp_path / "other"
        other.mkdir()
        import subprocess

        subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True)
        # Redirect only the worktree's Git link; no source is removed.
        (Path(run["worktree"]) / ".git").write_text(f"gitdir: {other / '.git'}\n")
    with pytest.raises(PipelineError):
        verify_branch(svc.store.get(rid), sha)
    with pytest.raises(PipelineError):
        svc.approve(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"
    assert (Path(run["worktree"]) / "new.py").read_text() == "value = 1\n"
    assert (repo / "main.py").read_text() == "developer local edits\n"


def test_post_approval_drift_is_visible_and_cleanup_does_not_repair_it(service):
    svc, rid, repo = ready(service, True)
    svc.approve(rid)
    run = svc.store.get(rid)
    git = Git(Path(run["worktree"]))
    (Path(run["worktree"]) / "after.py").write_text("after = True\n")
    changed = git.commit("승인 이후 사용자 변경")
    value = svc.queries.snapshot(rid)
    assert value["handoff"]["approved_sha"] == run["final_sha"]
    assert value["handoff"]["current_branch_sha"] == changed
    assert value["handoff"]["status"] == "changed"
    assert value["validation"]["status"] == "invalid"
    with pytest.raises(PipelineError, match="branch differs"):
        svc.cleanup(rid)
    assert Path(run["worktree"]).exists()
    assert Git(repo).baseline(run["branch"]) == changed


def test_queue_capacity_view_is_readonly_and_exposes_only_counts(service):
    from dev_orchestra.application.queue import QueueService
    from dev_orchestra.infrastructure.coordination import configure_capacity

    svc, rid, repo = service
    upgrade(svc.store.path)
    run = svc.store.get(rid)
    other = svc.create(repo, Path(run["directory"]) / "spec.md")
    QueueService(svc.store).reserve(rid)
    assert svc.queries.snapshot(rid)["capacity"]["wait_reason"] is None
    version = svc.store.get(other)["version"]
    before = set(repo.rglob("*"))
    view = svc.queries.snapshot(other)
    assert view["capacity"]["active"] == 1
    assert view["capacity"]["wait_reason"] == "total_capacity"
    assert svc.store.get(other)["version"] == version
    assert before == set(repo.rglob("*"))
    configure_capacity(svc.store.path, total=2)
    view = svc.queries.snapshot(other)
    assert view["capacity"]["wait_reason"] == "repository_capacity"
    assert set(view["capacity"]) == {
        "active",
        "total",
        "repository_active",
        "repository_limit",
        "wait_reason",
    }


def test_shared_resource_wait_marker_is_scoped_and_removed(service, tmp_path):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from dev_orchestra.application.progress import capacity_view
    from dev_orchestra.infrastructure.coordination import slot

    svc, rid, _ = service
    svc.store.update(rid, state="testing", next_role="tester")
    run = svc.store.get(rid)
    resource = tmp_path / "shared-loopback"
    owner = tmp_path / "resource-owner"
    owner.mkdir()
    release = threading.Event()
    entered = threading.Event()

    def wait():
        with slot(resource, Path(run["directory"]), limit=1):
            entered.set()
            release.wait(5)

    with ThreadPoolExecutor(1) as executor:
        with slot(resource, owner, limit=1):
            future = executor.submit(wait)
            marker = Path(run["directory"]) / "capacity-wait.json"
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert marker.is_file()
            assert capacity_view(svc.store, svc.store.get(rid))["wait_reason"] == "shared_resource"
            assert not entered.is_set()
        assert entered.wait(5)
        assert not marker.exists()
        release.set()
        future.result(5)


def test_handoff_compares_exact_ref_without_peeling_tag_objects(service):
    svc, rid, repo = service
    run = svc.store.get(rid)
    git = Git(repo)
    git.call("tag", "-a", "inspection-tag", "-m", "검사 태그", run["base_sha"])
    tag = git.call("rev-parse", "refs/tags/inspection-tag")
    ref = git.common() / "refs" / "heads" / run["branch"]
    ref.parent.mkdir(parents=True, exist_ok=True)
    # Simulate external corruption without letting Git repair or peel the ref.
    ref.write_text(tag + "\n")
    with pytest.raises(PipelineError, match="branch differs"):
        verify_branch(run, run["base_sha"], worktree=False)
    value = handoff(
        run | {"state": "completed", "final_sha": run["base_sha"], "worktree_removed": True}
    )
    assert value["status"] == "changed"
    assert value["current_branch_sha"] == tag
    assert value["approved_sha"] == run["base_sha"]
    assert ref.read_text().strip() == tag
