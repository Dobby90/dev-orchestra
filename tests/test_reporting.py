"""Report presentation regressions using disposable repositories, without model calls."""

from pathlib import Path

from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.application.results import Results
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.orchestration.runner import Runner


def test_report_summary_evidence_and_approval_state(service):
    svc, rid, _ = service
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    text = svc.report(rid)
    assert "승인 대기" in text
    assert "| AC1 | greeting works | unit | 연결 명령 통과 |" in text
    assert "| unit | test | passed | 0 | 1 | 확인 불가 |" in text
    assert text.index("## 작업 결과 요약") < text.index("## 부록: 원본")
    run = svc.store.get(rid)
    assert f"생성 버전: {run['version']}" in text
    assert f"보고서 대상 SHA: `{run['final_sha']}`" in text
    svc.approve(rid)
    approved = svc.report(rid)
    assert "승인 완료" in approved and "승인 대기" not in approved
    assert "Human approval is required" not in approved


def test_report_uses_candidate_not_head_and_rejects_stale_coverage(service):
    svc, rid, _ = service
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    tree = Path(run["worktree"])
    (tree / "after.py").write_text("after = True\n")
    Git(tree).commit("후속 변경")
    svc.store.update(rid, revision=run.get("revision", 0) + 1)
    text = svc.report(rid)
    assert "통과 근거 미확인" in text
    assert "after.py" not in text


def test_report_preserves_snapshot_after_cleanup_and_old_report(service):
    svc, rid, _ = service
    upgrade(svc.store.path)
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    tree = Path(run["worktree"])
    (tree / "feature.py").write_text("feature = True\n")
    sha = Git(tree).commit("보고서 스냅샷")
    svc.store.update(rid, current_sha=sha, final_sha=sha)
    Results(svc.store).save_snapshot(rid, sha)
    old = Results(svc.store).report(rid)
    # Simulate an unavailable Worktree without deleting any repository files.
    svc.store.update(rid, worktree=str(tree / "missing"))
    text = svc.report(rid)
    assert "| A | feature.py |" in text
    old_path, _ = Results(svc.store).artifact(rid, old["artifact_id"])
    assert old_path.read_text() == old["markdown"]


def test_early_failure_is_not_reported_as_success(service):
    svc, rid, _ = service
    svc.store.update(rid, "failed", error="preflight failed")
    text = svc.report(rid)
    assert "검증 미완료" in text
    assert "실제 실행 결과가 없습니다." in text
    assert "preflight failed" in text
    assert "승인 대기" not in text


def test_report_formats_counts_review_states_and_redacts_secrets(service):
    svc, rid, _ = service
    run = svc.store.get(rid)
    finding = {
        "severity": "high",
        "file": "main.py",
        "line": 1,
        "description": "wrong | result",
        "status": "resolved",
        "resolution_evidence": "rechecked",
    }
    svc.store.update(
        rid,
        test_result={
            "execution": {
                "commands": [
                    {
                        "command_id": "unit",
                        "kind": "test",
                        "total": 7,
                        "failed": 1,
                        "skipped": 2,
                        "status": "failed",
                        "exit_code": 1,
                    }
                ]
            }
        },
        review_result={
            "summary": "token=CANARY",
            "issues": [finding],
            "issue_states": [finding],
        },
    )
    text = svc.report(rid)
    assert "| unit | test | failed | 1 | 7 | 4 | 1 | 2 |" in text
    assert text.count("wrong &#124; result") == 1
    assert "| resolved |" in text and "rechecked" in text
    assert "CANARY" not in text
    assert run["base_sha"] in text
