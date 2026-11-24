"""Explain static fixes awaiting evidence without weakening the SHA-bound approval gate."""

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.agents.prompts import continuation_prompt, prompt
from dev_orchestra.application.reviews import Reviews
from dev_orchestra.orchestration.runner import Runner


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


@pytest.mark.parametrize("same_sha", [True, False])
def test_fixer_receives_latest_unresolved_assessment_for_existing_issue(service, same_sha):
    svc, rid, _ = service
    svc.store.update(rid, revision=2)
    evidence = "HTTP 500 회귀 테스트가 통과했습니다. HTTP 405에서는 오류 알림이 계속 표시됩니다."

    class Agent(FakeAgent):
        def run(self, request, directory, stop):
            result = super().run(request, directory, stop)
            if request.role == "reviewer" and request.iteration == 1:
                review_prompt = prompt(request)
                assert "Keep the existing issue_id" in review_prompt
                assert "Reserve issues for newly discovered independent defects" in review_prompt
                result.issues = []
                result.assessments[0].evidence = evidence
                if not same_sha:
                    result.assessments[0].candidate_sha = "0" * 40
            if request.role == "fixer":
                (issue,) = request.context["open_issues"]
                assert issue["status"] == "unresolved"
                if request.iteration == 1:
                    self.issue_id = issue["issue_id"]
                    assert "latest_assessment" not in issue
                else:
                    assert issue["issue_id"] == self.issue_id
                    if same_sha:
                        assert issue["latest_assessment"] == {
                            "candidate_sha": request.input_sha,
                            "revision": 2,
                            "reviewer_status": "unresolved",
                            "evidence": evidence,
                        }
                        assert evidence in prompt(request)
                        assert evidence in continuation_prompt(request)
                    else:
                        assert "latest_assessment" not in issue
            return result

    agent = Agent(block=2)
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert run["iteration"] == 2
    (issue,) = Reviews(svc.store).issues(rid)
    assert issue["issue_id"] == agent.issue_id
    assert issue["status"] == "resolved"
    svc.approve(rid)
