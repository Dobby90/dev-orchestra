"""Deterministic Markdown presentation of stored run results."""

from pathlib import Path

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.results import Results
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.git import Git


def cell(value):
    if value is None:
        return "확인 불가"
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("\r", " ")
        .replace("\n", " / ")
    )


def table(headers, rows):
    return "\n".join(
        ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
        + ["| " + " | ".join(cell(v) for v in row) + " |" for row in rows]
    )


def result_markdown(store, run, steps):
    tests = run.get("test_result") or {}
    review = run.get("review_result") or {}
    sha = run.get("final_sha") or run.get("current_sha") or run.get("base_sha")
    revision = run.get("revision", 0)
    approval = {
        "waiting_approval": "승인 대기 — 최종 Diff와 검증 근거를 확인한 뒤 승인하세요.",
        "completed": "승인 완료 — 승인한 커밋과 로컬 브랜치를 확인하세요.",
    }.get(run["state"], "검증 미완료 — 현재 오류·질문과 미실행 검사를 확인하세요.")
    content = "# DevOrchestra 작업 최종 보고서\n\n## 작업 결과 요약\n\n" + approval
    content += (
        f"\n\nRun ID: `{run['id']}` · 생성 버전: {run['version']} · revision: {revision}"
        f"\n\n보고서 대상 SHA: `{sha}`\n\n"
        "이 문서는 생성 시점의 기록입니다. 이후 변경은 반영하지 않습니다.\n"
    )
    summaries = []
    results = Results(store)
    for step in steps:
        if (
            step["role"] in {"developer", "fixer"}
            and step["status"] == "completed"
            and step.get("revision", 0) == revision
        ):
            try:
                payload = results.payload(run, step)
                summaries.append((step["role"], payload.get("summary", "확인 불가")))
            except PipelineError:
                summaries.append((step["role"], "저장된 구현 설명을 확인할 수 없습니다."))
    content += "\n### 구현 설명 (역할 보고, 검증 결과와 별도)\n\n"
    content += table(["역할", "설명"], summaries) if summaries else "저장된 구현 설명이 없습니다."
    commands = tests.get("execution", {}).get("commands", [])
    by_id = {c["command_id"]: c for c in commands}
    bound = tests.get("sha") == sha and tests.get("revision", 0) == revision
    coverage = {c["criterion_id"]: c["command_ids"] for c in tests.get("coverage", [])}
    content += "\n\n## 요구사항별 검증\n\n"
    rows = []
    for criterion in run.get("criteria", []):
        ids = coverage.get(criterion["id"], [])
        passed = bound and bool(ids) and all(by_id.get(i, {}).get("passed") is True for i in ids)
        rows.append(
            (
                criterion["id"],
                criterion["description"],
                ", ".join(ids) or "없음",
                "연결 명령 통과" if passed else "통과 근거 미확인",
            )
        )
    content += (
        table(["인수 조건", "내용", "연결 명령", "결과"], rows)
        if rows
        else "저장된 인수 조건이 없습니다."
    )
    content += (
        "\n\n연결 명령의 통과는 요구사항의 완전한 충족을 보장하지 않습니다.\n\n## 테스트 결과\n\n"
    )
    content += (
        f"근거 SHA: `{tests.get('sha') or '없음'}` · revision: {tests.get('revision', 0)}\n\n"
    )
    content += (
        table(
            ["명령", "종류", "상태", "종료 코드", "전체", "통과", "실패", "건너뜀", "시간(초)"],
            [
                (
                    c["command_id"],
                    c.get("kind", "test"),
                    c.get("status"),
                    c.get("exit_code"),
                    c.get("total"),
                    c["total"] - c["failed"] - c["skipped"]
                    if c.get("kind", "test") == "test"
                    and all(type(c.get(k)) is int for k in ("total", "failed", "skipped"))
                    else None,
                    c.get("failed"),
                    c.get("skipped"),
                    c.get("duration_seconds"),
                )
                for c in commands
            ],
        )
        if commands
        else "실제 실행 결과가 없습니다."
    )
    content += "\n\n## 리뷰와 남은 문제\n\n"
    content += (
        f"근거 SHA: `{review.get('sha') or '없음'}` · revision: {review.get('revision', 0)}\n\n"
    )
    content += cell(review.get("summary") or "리뷰 결과가 없습니다.") + "\n\n"
    issues = review.get("issue_states", review.get("issues", []))
    content += (
        table(
            ["심각도", "위치", "상태", "문제", "해결 근거"],
            [
                (
                    i["severity"],
                    f"{i['file']}:{i['line']}",
                    i.get("status", "unresolved"),
                    i["description"],
                    i.get("resolution_evidence"),
                )
                for i in issues
            ],
        )
        if issues
        else "저장된 리뷰 지적이 없습니다. 리뷰 미실행은 지적 없음과 다릅니다."
    )
    content += "\n\n미실행·실패 검사와 미해결 지적은 확인이 필요합니다.\n\n## 변경 파일\n\n"
    try:
        snapshot = results.changes(run["id"], sha, revision) if results.enabled() else None
    except ApplicationError:
        snapshot = None
    except (PipelineError, OSError, ValueError):
        return content + "보존된 변경 파일 근거를 확인할 수 없습니다.\n"
    if snapshot:
        content += table(["상태", "파일"], [(f["status"], f["path"]) for f in snapshot["files"]])
    elif Path(run["worktree"]).exists():
        try:
            files = Git(Path(run["worktree"])).call(
                "diff", "--name-only", f"{run['base_sha']}...{sha}", "--"
            )
            content += (
                table(["파일"], [(f,) for f in files.splitlines()])
                if files
                else "변경 파일이 없습니다."
            )
        except PipelineError:
            content += "변경 파일을 확인할 수 없습니다."
    else:
        content += "보존된 변경 파일 근거가 없습니다."
    return content + "\n"
