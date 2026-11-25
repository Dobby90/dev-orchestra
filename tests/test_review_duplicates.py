"""Duplicate links change presentation without rewriting review evidence."""

import json
from pathlib import Path

import pytest
from test_workflow import service as service
from typer.testing import CliRunner

from dev_orchestra.application.commands import Commands
from dev_orchestra.application.results import Results
from dev_orchestra.application.reviews import Reviews
from dev_orchestra.cli import app
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.orchestration.runner import transition_summary


def save_review(svc, rid, iteration, *, issues=(), assessments=()):
    run = svc.store.get(rid)
    sha = run["base_sha"]
    step, _ = svc.store.start_step(rid, "reviewer", iteration, sha)
    payload = {
        "sha": sha,
        "revision": run.get("revision", 0),
        "schema_version": 2,
        "summary": "독립 검토",
        "issues": list(issues),
        "assessments": list(assessments),
    }
    payload["issue_states"] = Reviews(svc.store).apply(run, step, payload)
    artifact = Path(run["directory"]) / f"review-{step}.json"
    checksum = write(artifact, payload)
    svc.store.finish_step(step, sha, {"artifact": str(artifact), "hash": checksum})
    svc.store.update(rid, review_result=payload)
    return step, artifact


def finding(number, severity):
    return {
        "id": f"finding-{number}",
        "severity": severity,
        "file": "main.py",
        "line": number,
        "description": f"같은 결함의 발견 {number}",
        "evidence": "이전 요청의 오류 알림이 표시됨",
        "suggestion": "현재 요청인지 확인",
    }


def assessment(issue_id, status, sha):
    return {
        "issue_id": issue_id,
        "status": status,
        "candidate_sha": sha,
        "evidence": "HTTP 500은 통과했으며 HTTP 405 경로를 재평가함",
    }


def link(svc, rid, duplicate_id, representative_id, *, reason="실패 조건과 근거를 확인함"):
    return Commands(svc).execute(
        rid,
        "link-review-duplicate",
        svc.store.get(rid)["version"],
        {"duplicate_id": duplicate_id, "representative_id": representative_id, "reason": reason},
    )


def test_link_preserves_history_and_resolution_gate(service):
    svc, rid, _ = service
    run = svc.store.get(rid)
    sha = run["base_sha"]
    svc.store.update(rid, revision=2)
    reviews = Reviews(svc.store)
    old_step, old_artifact = save_review(svc, rid, 0, issues=[finding(1, "medium")])
    save_review(svc, rid, 1, issues=[finding(2, "high")])
    representative, duplicate = [issue["issue_id"] for issue in reviews.issues(rid)]
    recent_step, recent_artifact = save_review(
        svc, rid, 2, assessments=[assessment(duplicate, "unresolved", sha)]
    )
    originals = [path.read_bytes() for path in (old_artifact, recent_artifact)]
    ledger = reviews.issues(rid)

    cli = CliRunner()
    prefix = ["--database", str(svc.store.path)]
    operation = cli.invoke(
        app,
        [*prefix, "link-review-duplicate", rid, duplicate, representative, "--reason", "동일 결함"],
    )
    assert operation.exit_code == 0, operation.output
    listing = cli.invoke(app, [*prefix, "review-issues", rid])
    assert listing.exit_code == 0, listing.output
    assert json.loads(listing.output) == {
        "unresolved_count": 1,
        "duplicates": {duplicate: representative},
        "issues": ledger,
    }

    run = svc.store.get(rid)
    assert "미해결 1건 (차단 1건)" in svc.report(rid)
    assert transition_summary(run | {"state": "reviewing"}, "fixing")["review_issues"] == {
        "high": 1
    }
    (issue,) = reviews.fixer_issues(run)
    assert issue["issue_id"] == representative and issue["severity"] == "high"
    assert issue["evidence_issue_id"] == duplicate
    assert issue["latest_assessment"] == {
        "candidate_sha": sha,
        "revision": 2,
        "reviewer_status": "unresolved",
        "evidence": assessment(duplicate, "unresolved", sha)["evidence"],
    }
    assert [i["issue_id"] for i in issue["related_unresolved_issues"]] == [representative]
    assert [i["issue_id"] for i in reviews.open(rid)] == [representative, duplicate]
    assert [path.read_bytes() for path in (old_artifact, recent_artifact)] == originals
    assert len(Results(svc.store).reviews(rid, old_step)["issues"]) == 1
    assert len(Results(svc.store).reviews(rid, recent_step)["issues"]) == 2

    save_review(svc, rid, 3, assessments=[assessment(duplicate, "resolved", sha)])
    assert len(reviews.open(rid)) == 2
    assert "미해결 1건 (차단 1건)" in svc.report(rid)

    svc.store.update(rid, test_result={"sha": sha, "revision": 2, "execution": {"passed": True}})
    save_review(svc, rid, 4, assessments=[assessment(representative, "resolved", sha)])
    (issue,) = reviews.fixer_issues(svc.store.get(rid))
    assert (issue["issue_id"], issue["evidence_issue_id"]) == (representative, duplicate)
    assert "미해결 1건 (차단 1건) · 해결 1건" in svc.report(rid)

    save_review(svc, rid, 5, assessments=[assessment(duplicate, "resolved", sha)])
    assert not reviews.fixer_issues(svc.store.get(rid))
    assert "미해결 0건 (차단 0건) · 해결 2건" in svc.report(rid)


def test_linking_groups_keeps_direct_links_and_rejects_invalid_ids(service):
    svc, rid, _ = service
    save_review(svc, rid, 0, issues=[finding(i, "high") for i in range(1, 4)])
    reviews = Reviews(svc.store)
    duplicate, first, second = [i["issue_id"] for i in reviews.issues(rid)]
    with pytest.raises(PipelineError, match="belong to this run"):
        link(svc, rid, duplicate, "issue-from-another-run")
    with pytest.raises(PipelineError, match="itself"):
        link(svc, rid, first, first)
    with lock(Path(svc.store.get(rid)["directory"]) / "execution.lock"):
        with pytest.raises(OwnershipError, match="execution is already owned"):
            link(svc, rid, duplicate, first)

    link(svc, rid, duplicate, first)
    with pytest.raises(PipelineError, match="representative ID as the target"):
        link(svc, rid, first, duplicate)
    link(svc, rid, first, second)
    link(svc, rid, duplicate, second)
    assert svc.store.get(rid)["review_issue_duplicates"] == {duplicate: second, first: second}
    assert len(reviews.open(rid)) == 3
    (issue,) = reviews.fixer_issues(svc.store.get(rid))
    assert issue["issue_id"] == second
    events = svc.store.events(rid)
    assert sum(event["kind"] == "issue.duplicate_linked" for event in events) == 2
