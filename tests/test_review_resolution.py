"""Explain static fixes awaiting evidence without weakening the SHA-bound approval gate."""

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.agents.contracts import ResponseError
from dev_orchestra.agents.prompts import continuation_prompt, prompt
from dev_orchestra.application.reviews import Reviews, validate_review_findings
from dev_orchestra.domain.models import Issue, ReviewIssueDistinction, ReviewResultV2
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
            if request.role == "reviewer" and request.iteration == 0:
                self.original_finding = result.issues[0]
            if request.role == "reviewer" and request.iteration == 1:
                review_prompt = prompt(request)
                assert "Keep the existing issue_id" in review_prompt
                assert "Reserve issues for newly discovered independent defects" in review_prompt
                result.issues = []
                result.assessments[0].evidence = evidence
                feedback = request.context.get("response_feedback")
                if same_sha and not feedback:
                    result.issues = [
                        self.original_finding.model_copy(
                            update={
                                "id": "duplicate",
                                "description": "bug.",
                                "evidence": evidence,
                                "line": 2,
                            }
                        )
                    ]
                elif same_sha:
                    assert self.issue_id in feedback["error"]
                    assert "use assessments" in feedback["error"]
                    with svc.store.connection() as db:
                        assert (
                            db.execute("SELECT COUNT(*) FROM issue_assessments").fetchone()[0] == 0
                        )
                else:
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
    review_attempts = [
        s["status"] for s in svc.store.steps(rid) if s["role"] == "reviewer" and s["iteration"] == 1
    ]
    assert review_attempts == (["failed", "completed"] if same_sha else ["completed"])
    svc.approve(rid)


def test_same_line_can_have_two_independent_review_findings(service):
    svc, rid, _ = service
    run = svc.store.get(rid)
    sha = run["base_sha"]
    reviews = Reviews(svc.store)
    original = Issue(
        id="old",
        severity="high",
        file="main.py",
        line=1,
        description="A previous request can emit a notification",
        evidence="A shared callback performs both side effects",
        suggestion="Check request ownership before notifying",
    )
    step, _ = svc.store.start_step(rid, "reviewer", 0, sha)
    (old,) = reviews.apply(run, step, {"sha": sha, "issues": [original.model_dump()]})
    new = original.model_copy(
        update={
            "id": "new",
            "description": "A failed request incorrectly clears the session",
        }
    )
    independent = new.model_copy(update={"evidence": "Session deletion runs on HTTP 500."})
    validate_review_findings(ReviewResultV2(summary="review", issues=[independent]), [old], sha)

    result = ReviewResultV2(summary="review", issues=[new])
    with pytest.raises(ResponseError, match="issue_distinctions"):
        validate_review_findings(result, [old], sha)
    result.issue_distinctions = [
        ReviewIssueDistinction(
            finding_id="new",
            existing_issue_id=old["issue_id"],
            reason="The callback also deletes the session in response to HTTP 500, independently of notifications.",
        )
    ]
    validate_review_findings(result, [old], sha)

    step, _ = svc.store.start_step(rid, "reviewer", 1, sha)
    saved = reviews.apply(run, step, result.model_dump() | {"sha": sha})
    assert len({item["issue_id"] for item in saved}) == 2
    assert {item["description"] for item in saved} == {original.description, new.description}


def test_similar_finding_requests_reassessment_even_with_changed_evidence():
    original = Issue(
        id="old",
        severity="high",
        file="main.py",
        line=53,
        description="기간 변경 후 이전 시뮬레이션 오류가 전역 토스트로 표시됩니다. R04-3을 위반합니다.",
        evidence="HTTP 500과 405의 오류 처리가 토스트를 표시합니다.",
        suggestion="현재 요청인지 확인한 뒤 알립니다.",
    )
    new = original.model_copy(
        update={
            "id": "new",
            "line": 61,
            "description": "기간 변경 후 이전 시뮬레이션 오류가 전역 토스트로 표시됩니다. R04-3 위반입니다.",
            "evidence": "HTTP 500은 통과하나 405 처리 분기가 여전히 알립니다.",
        }
    )
    old = original.model_dump() | {"issue_id": "issue-existing"}
    with pytest.raises(ResponseError, match="use assessments"):
        validate_review_findings(ReviewResultV2(summary="review", issues=[new]), [old], "0" * 40)
