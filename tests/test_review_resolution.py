"""Explain static fixes awaiting evidence without weakening the SHA-bound approval gate."""

import pytest
from test_workflow import service as service

from dev_orchestra.application.reviews import Reviews


@pytest.mark.parametrize(
    "passed,same_sha,same_revision,expected",
    [
        (False, True, True, "unresolved"),
        (True, False, True, "unresolved"),
        (True, True, False, "unresolved"),
        (True, True, True, "resolved"),
    ],
)
def test_review_resolution_requires_matching_successful_test_evidence(
    service, passed, same_sha, same_revision, expected
):
    svc, rid, _ = service
    run = svc.store.get(rid)
    sha = run["base_sha"]
    step, _ = svc.store.start_step(rid, "reviewer", 0, sha)
    reviews = Reviews(svc.store)
    finding = {
        "severity": "high",
        "file": "main.py",
        "line": 1,
        "description": "invalid result",
        "evidence": "wrong return value",
        "suggestion": "fix result",
    }
    issue = reviews.apply(run, step, {"sha": sha, "issues": [finding]})[0]
    svc.store.update(
        rid,
        test_result={
            "sha": sha if same_sha else "0" * 40,
            "revision": 0 if same_revision else 1,
            "execution": {"passed": passed},
        },
    )
    step, _ = svc.store.start_step(rid, "reviewer", 1, sha)
    value = reviews.apply(
        svc.store.get(rid),
        step,
        {
            "sha": sha,
            "issues": [],
            "assessments": [
                {
                    "issue_id": issue["issue_id"],
                    "candidate_sha": sha,
                    "status": "resolved",
                    "evidence": "source fix and regression assertion reviewed",
                }
            ],
        },
    )[0]
    assert value["status"] == expected
    if expected == "resolved":
        assert value["resolution_evidence"] and value["status_note"] is None
    else:
        assert value["resolution_evidence"] is None
        assert value["status_note"] == "코드 수정 확인, 동일 SHA·revision 테스트 통과 근거 대기"


def test_unresolved_reassessment_clears_old_pending_fix_note(service):
    svc, rid, _ = service
    run = svc.store.get(rid)
    sha = run["base_sha"]
    step, _ = svc.store.start_step(rid, "reviewer", 0, sha)
    reviews = Reviews(svc.store)
    finding = {
        "severity": "medium",
        "file": "main.py",
        "line": 1,
        "description": "bug",
        "evidence": "wrong result",
        "suggestion": "fix",
    }
    issue = reviews.apply(run, step, {"sha": sha, "issues": [finding]})[0]
    for iteration, status in [(1, "resolved"), (2, "unresolved")]:
        step, _ = svc.store.start_step(rid, "reviewer", iteration, sha)
        value = reviews.apply(
            run,
            step,
            {
                "sha": sha,
                "issues": [],
                "assessments": [
                    {
                        "issue_id": issue["issue_id"],
                        "candidate_sha": sha,
                        "status": status,
                        "evidence": "current candidate reviewed",
                    },
                ],
            },
        )[0]
    assert value["status"] == "unresolved" and value["status_note"] is None
