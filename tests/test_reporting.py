"""Report presentation regressions using disposable repositories, without model calls."""

import json
from pathlib import Path

from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service
from typer.testing import CliRunner

from dev_orchestra.application.results import Results
from dev_orchestra.cli import app
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.orchestration.runner import Runner


def test_report_summary_evidence_and_approval_state(service):
    svc, rid, _ = service
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    text = svc.report(rid)
    assert "승인 대기" in text
    assert [line for line in text.splitlines() if line.startswith("## ")] == [
        "## 변경 요약",
        "## 검증 결과",
        "## 승인 전 확인",
    ]
    assert "1개 중 1개 연결 명령 통과" in text
    assert "명령 1개 중 1개 통과" in text
    assert "코드 리뷰** · 미해결 0건 (차단 0건)" in text
    run = svc.store.get(rid)
    assert run["spec"] not in text and "command_id" not in text
    assert f"생성 버전 {run['version']}" in text
    assert f"대상 SHA `{run['final_sha']}`" in text
    svc.approve(rid)
    approved = svc.report(rid)
    assert "승인 완료" in approved and "승인 대기" not in approved
    assert "Human approval is required" not in approved


def test_cli_detailed_report_reads_evidence_without_rewriting_summary(service):
    svc, rid, _ = service
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    saved = Results(svc.store).report(rid)
    path = Path(run["directory"]) / "final-report.md"
    original = path.read_bytes()
    result = CliRunner().invoke(
        app, ["--database", str(svc.store.path), "report", rid, "--detailed"]
    )
    assert result.exit_code == 0, result.output
    assert run["spec"] in result.output
    assert '"command_id": "unit"' in result.output
    assert "## 구현·검증 이력" in result.output
    assert path.read_bytes() == original
    assert Results(svc.store).report(rid) == saved


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
    assert text.count("대상 SHA·revision과 근거 불일치") == 2
    assert "명령 1개 중 1개 통과" not in text
    assert "차단 0건" not in text
    assert "after.py" not in svc.report(rid, detailed=True)


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
    assert "- 변경 파일 1개" in text
    assert "feature.py" not in text
    assert "- `feature.py`" in svc.report(rid, detailed=True)
    old_path, _ = Results(svc.store).artifact(rid, old["artifact_id"])
    assert old_path.read_text() == old["markdown"]


def test_early_failure_is_not_reported_as_success(service):
    svc, rid, _ = service
    svc.store.update(rid, "failed", error="preflight failed")
    text = svc.report(rid)
    assert "검증 미완료" in text
    assert "실제 실행 결과 없음" in text
    assert "리뷰 결과 없음" in text
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
        test_scope="affected",
        test_result={
            "sha": run["base_sha"],
            "test_selection": {"actual_scope": "full", "excluded": []},
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
                        "error": "exit code 1: token=CANARY",
                    }
                ]
            },
        },
        review_result={
            "sha": run["base_sha"],
            "summary": "token=CANARY",
            "issues": [finding],
            "issue_states": [finding],
        },
        test_change_records=[
            {"changes": [{"file": "tests/test_main.py", "reason": "새 기대값 확인"}]}
        ],
    )
    text = svc.report(rid)
    assert "요청 변경 영향 범위 · 실제 전체 · 명령 1개 중 0개 통과" in text
    assert "건너뛴 테스트 2건" in text
    assert "테스트·검사 `unit` · 실패" in text
    assert "해결 1건" in text and "rechecked" not in text
    assert "기존 테스트 변경 `tests/test_main.py` · 새 기대값 확인" in text
    assert "wrong | result" not in text
    assert "CANARY" not in text
    detailed = svc.report(rid, detailed=True)
    assert "wrong | result" in detailed and "rechecked" in detailed
    assert "CANARY" not in detailed
    evidence = json.loads(detailed.split("```json\n")[1].split("\n```")[0])
    assert (
        evidence["tests"]["execution"]["commands"][0]["error"] == 'exit code 1: token="[REDACTED]"'
    )
    assert run["base_sha"] in text


def test_report_explains_why_a_reviewed_fix_is_still_unresolved(service):
    svc, rid, _ = service
    run = svc.store.get(rid)
    note = "코드 수정 확인, 동일 SHA·revision 테스트 통과 근거 대기"
    svc.store.update(
        rid,
        review_result={
            "sha": run["base_sha"],
            "summary": "수정 검토",
            "issue_states": [
                {
                    "severity": "medium",
                    "file": "main.py",
                    "line": 1,
                    "description": "입력 검증",
                    "status": "unresolved",
                    "resolution_evidence": None,
                    "status_note": note,
                }
            ],
        },
    )
    text = svc.report(rid)
    assert "미해결 1건 (차단 0건)" in text
    assert f"리뷰 medium · `main.py:1` · 입력 검증 · {note}" in text
