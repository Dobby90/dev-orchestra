"""Markdown summaries and on-demand details for stored run results."""

import json
from html import escape
from pathlib import Path

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.handoff import handoff_markdown
from dev_orchestra.application.progress import role_progress
from dev_orchestra.application.results import Results, test_case_counts
from dev_orchestra.config import Review
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.process import redact, sanitize


def excerpt(value, limit=240):
    text = " ".join(redact(value).split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return escape(text, quote=False)


def changed_files(results, run, sha, revision):
    try:
        if results.enabled() and sha:
            return [f["path"] for f in results.changes(run["id"], sha, revision)["files"]]
    except ApplicationError as error:
        if error.code != "SNAPSHOT_NOT_FOUND":
            return None
    except (PipelineError, OSError, ValueError):
        return None
    if sha and run.get("base_sha") and run.get("worktree") and Path(run["worktree"]).exists():
        try:
            names = Git(Path(run["worktree"])).call(
                "diff", "--name-only", f"{run['base_sha']}...{sha}", "--"
            )
            return names.splitlines()
        except PipelineError:
            pass
    return None


def evidence_matches(payload, sha, revision):
    return bool(sha) and payload.get("sha") == sha and payload.get("revision", 0) == revision


def report_test_evidence(results, run, steps, sha, revision):
    latest = next(
        (
            step
            for step in reversed(steps)
            if step["role"] == "tester" and step.get("revision", 0) == revision
        ),
        None,
    )
    # Unusable test execution evidence is stored on the failed step, before advance().
    if latest and latest["status"] == "failed" and latest["output_sha"] == sha:
        try:
            return results.payload(run, latest)
        except PipelineError:
            return {}
    return run.get("test_result") or {}


def test_summary(run, tests, sha, revision, checks):
    commands = tests.get("execution", {}).get("commands", [])
    bound = evidence_matches(tests, sha, revision)
    passed = {c["command_id"] for c in commands if c.get("passed") is True}
    scopes = {"full": "전체", "affected": "변경 영향 범위"}
    status_labels = {
        "failed": "실패",
        "timeout": "시간 초과",
        "parse_error": "JUnit 파싱 실패",
        "unavailable": "실행 근거 확인 불가",
        "not_run": "미실행",
    }
    if not tests:
        summary = "실제 실행 결과 없음"
    elif not bound:
        summary = "확인 불가 (대상 SHA·revision과 근거 불일치)"
    elif not commands:
        summary = "실제 실행 결과 없음"
    else:
        selection = tests.get("test_selection") or {}
        requested = scopes.get(run.get("test_scope", "full"), "확인 불가")
        actual = scopes.get(selection.get("actual_scope"), "확인 불가")
        summary = (
            f"요청 {requested} · 실제 {actual} · 명령 {len(commands)}개 중 {len(passed)}개 통과"
        )
        if excluded := selection.get("excluded"):
            summary += f" · 범위에서 제외된 명령 {len(excluded)}개"
        if skipped := test_case_counts(commands)["skipped"]:
            summary += f" · 건너뛴 테스트 {skipped}건"
        for command in commands:
            if command["command_id"] in passed:
                continue
            status = status_labels.get(command.get("status"), "통과 근거 미확인")
            item = f"테스트·검사 `{command['command_id']}` · {status}"
            if command.get("error"):
                item += f" · {excerpt(command['error'])}"
            checks.append(item)

    coverage = {c["criterion_id"]: c["command_ids"] for c in tests.get("coverage", [])}
    criteria = run.get("criteria", [])
    unverified = [
        c
        for c in criteria
        if not (bound and coverage.get(c["id"]) and passed.issuperset(coverage[c["id"]]))
    ]
    for criterion in unverified:
        checks.append(
            f"인수 조건 `{criterion['id']}` 통과 근거 미확인 · {excerpt(criterion['description'])}"
        )
    lines = [f"- **테스트** · {summary}"]
    if criteria:
        lines.append(
            f"- **인수 조건** · {len(criteria)}개 중 {len(criteria) - len(unverified)}개 연결 명령 통과"
        )
    return lines


def review_summary(results, run, review, sha, revision, checks):
    if not review:
        return "리뷰 결과 없음"
    if not evidence_matches(review, sha, revision):
        return "확인 불가 (대상 SHA·revision과 근거 불일치)"
    issues = review.get("issue_states", review.get("issues", []))
    if review.get("schema_version") == 2 and "issue_states" in review and results.enabled():
        issues = results.current_review_issues(run["id"], issues)
    unresolved = [i for i in issues if i.get("status", "unresolved") == "unresolved"]
    blocking = (
        run.get("config", {})
        .get("review", {})
        .get("blocking_severities", Review().blocking_severities)
    )
    for issue in unresolved:
        item = f"리뷰 {issue['severity']} · `{issue['file']}:{issue['line']}` · {excerpt(issue['description'])}"
        if issue.get("status_note"):
            item += f" · {excerpt(issue['status_note'])}"
        checks.append(item)
    return (
        f"미해결 {len(unresolved)}건 (차단 {sum(i['severity'] in blocking for i in unresolved)}건)"
        f" · 해결 {sum(i.get('status') == 'resolved' for i in issues)}건"
        f" · 사용자 제외 {sum(i.get('status') == 'dismissed' for i in issues)}건"
    )


def result_markdown(store, run, steps, *, detailed=False):
    sha = run.get("final_sha") or run.get("current_sha") or run.get("base_sha")
    revision = run.get("revision", 0)
    state = run["state"]
    status = {"waiting_approval": "승인 대기", "completed": "승인 완료"}.get(state, "검증 미완료")
    results = Results(store)
    files = changed_files(results, run, sha, revision)
    lines = [
        "# DevOrchestra 작업 최종 보고서",
        "",
        f"**{status}** · 대상 SHA `{sha or '확인 불가'}` · revision {revision} · 생성 버전 {run['version']}",
        "",
        "## 변경 요약",
        "",
        "- 변경 파일 수 확인 불가" if files is None else f"- 변경 파일 {len(files)}개",
    ]
    latest = {}
    for step in steps:
        if (
            step["role"] in {"developer", "fixer"}
            and step["status"] == "completed"
            and step.get("revision", 0) == revision
        ):
            latest[step["role"]] = step
    for role, label in (("developer", "구현"), ("fixer", "수정")):
        if role in latest:
            try:
                payload = results.payload(run, latest[role])
                summary = payload.get("summary")
                if summary:
                    lines.append(f"- **{label}** · {excerpt(summary)}")
            except PipelineError:
                lines.append(f"- **{label}** · 저장된 설명 확인 불가")

    checks = []
    tests = report_test_evidence(results, run, steps, sha, revision)
    lines += ["", "## 검증 결과", ""]
    lines += test_summary(run, tests, sha, revision, checks)
    review = review_summary(results, run, run.get("review_result") or {}, sha, revision, checks)
    lines.append(f"- **코드 리뷰** · {review}")

    if run.get("error"):
        checks.insert(0, f"실행 오류 · {excerpt(run['error'])}")
    checks.extend(f"답변 필요 · {excerpt(question)}" for question in run.get("questions") or [])
    test_changes = {
        change["file"]: change["reason"]
        for record in run.get("test_change_records", [])
        for change in record["changes"]
    }
    checks.extend(
        f"기존 테스트 변경 `{file}` · {excerpt(reason)}" for file, reason in test_changes.items()
    )
    if state == "waiting_approval":
        heading = "승인 전 확인"
        checks.append("최종 Diff와 검증 근거를 확인한 뒤 승인하세요.")
    elif state == "completed":
        heading = "인계"
        if run.get("branch"):
            approved_sha = run.get("final_sha") or "확인 불가"
            checks.append(f"로컬 브랜치 인계 · `{run['branch']}` · 승인 SHA `{approved_sha}`")
        else:
            checks.append("저장된 로컬 브랜치 정보가 없습니다.")
    else:
        heading = "확인할 항목"
        if not checks:
            checks.append("실행 상태와 테스트·리뷰 근거를 확인하세요.")
    lines += ["", f"## {heading}", "", *(f"- {item}" for item in checks)]
    summary = redact("\n".join(lines) + "\n")
    return summary + detail_markdown(store, run, steps, files, tests) if detailed else summary


def detail_markdown(store, run, steps, files, tests):
    lines = ["", "## 원문 요구사항", "", run["spec"]]
    if run.get("directives"):
        lines += ["", "## 사용자 수정 요청", "", *run["directives"]]
    if files:
        lines += ["", "## 변경 파일", "", *(f"- `{file}`" for file in files)]
    lines += ["", "## 구현·검증 이력", ""]
    for step in steps:
        lines.append(
            f"- {step['role']} · 회차 {step['iteration']} · 시도 {step['attempt']} · "
            f"{step['status']} · SHA {step['output_sha']}"
        )
        progress = role_progress(store, run, step)
        for commit in progress["commits"]:
            lines.append(f"  - 작업 단위 커밋 `{commit['sha']}` · {commit['summary']}")
        if progress["checkpoint_sha"]:
            lines.append(
                f"  - 마지막 복구 지점 `{progress['checkpoint_sha']}` · "
                f"보존 변경 {progress['preserved_changes']}개 · 재개 방식 {progress['recovery']}"
            )
    if run.get("branch"):
        lines.append(handoff_markdown(run))
    evidence = {
        "tests": tests or None,
        "review": run.get("review_result"),
        "test_changes": [record["changes"] for record in run.get("test_change_records", [])],
    }
    return (
        redact("\n".join(lines) + "\n\n")
        + "## 원본 테스트·리뷰 결과와 기존 테스트 변경 기록\n\n```json\n"
        + json.dumps(sanitize(evidence), indent=2, ensure_ascii=False)
        + "\n```\n"
    )
