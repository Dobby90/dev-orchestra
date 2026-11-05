"""Concrete descriptions reach real commits without changing checkpoint identity."""

from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service
from test_workspace import edit, start_session
from test_workspace import setup as setup

from dev_orchestra.domain.models import PipelineError, WorkspaceResult
from dev_orchestra.infrastructure.commit_messages import describe_commit
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.orchestration.runner import Runner


@pytest.mark.parametrize(
    "message",
    ["자동 개발: RUN_ID developer", "역할 작업 완료: developer", "테스트 실행 전 변경 확정"],
)
def test_generic_subject_is_rejected(message):
    with pytest.raises(PipelineError, match="concrete Korean subject"):
        describe_commit(message, "구체적인 본문", ["main.py"])


def test_legacy_response_fallback_describes_files():
    assert describe_commit("", "변경 이유", ["main.py"]) == "main.py 변경 반영\n\n변경 이유"


def test_patch_commit_uses_description_and_metadata(service):
    svc, rid, _ = service

    class Agent(FakeAgent):
        def run(self, request, directory, stop):
            result = super().run(request, directory, stop)
            if request.role == "developer":
                result.patch = (
                    "diff --git a/main.py b/main.py\n--- a/main.py\n+++ b/main.py\n"
                    "@@ -1 +1 @@\n-original\n+changed\n"
                )
                result.commit_message = "인사 응답 형식 변경\n\n요구사항에 맞게 응답을 변경합니다."
            return result

    Runner(svc.store, Agent(), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    message = Git(Path(run["worktree"])).call("log", "-1", "--format=%B")
    assert message.startswith("인사 응답 형식 변경\n\n요구사항에 맞게 응답을 변경합니다.")
    assert "DevOrchestra-Role: developer" in message
    assert "DevOrchestra-Run: " + rid in message


def test_remaining_edits_get_their_own_description_and_stable_retry(setup):
    session, _ = start_session(setup)
    edit(session, "main.py", "first\n")
    first = session.checkpoint_commit(
        "first",
        session.git.sha(),
        {"main.py": session.data["files"]["main.py"]["hash"]},
        "인사 출력 변경\n\n기본 인사를 제공합니다.",
        "첫 기능",
    )
    edit(session, "extra.py", "remaining\n")
    _, sha, commits = session.finish(
        WorkspaceResult(
            summary="전체 단계 요약", commit_message="추가 인사 처리\n\n추가 입력을 처리합니다."
        )
    )
    message = session.git.call("log", "-1", "--format=%B")
    assert message.startswith(
        "추가 인사 처리\n\n추가 입력을 처리합니다.\n\nDevOrchestra-Checkpoint:"
    )
    assert "전체 단계 요약" not in message
    assert len(commits) == 2 and first["sha"] != sha
    assert session.finish(WorkspaceResult(summary="다시 완료"))[1] == sha


def test_test_edits_description_is_committed_before_execution(setup):
    session, _ = start_session(setup, "tester")
    edit(session, "tests/test_added.py", "assert True\n")
    session._test_runner = FakeTests()
    session.run_tests(
        ["unit"], commit_message="빈 입력 회귀 테스트 추가\n\n빈 입력의 응답을 검증합니다."
    )
    message = session.git.call("log", "-1", "--format=%B")
    assert message.startswith("빈 입력 회귀 테스트 추가\n\n빈 입력의 응답을 검증합니다.")
    assert session.read_test_payload()["sha"] == session.git.sha()


def test_invalid_patch_message_is_repairable_before_editing():
    from dev_orchestra.agents.contracts import ResponseError, validate_role
    from dev_orchestra.domain.models import AgentResult

    with pytest.raises(ResponseError, match="concrete Korean subject"):
        validate_role(
            "developer",
            AgentResult(summary="수정", patch="pending patch", commit_message="자동 개발: RUN_ID"),
        )


def test_generic_checkpoint_is_rejected_without_staging(setup):
    session, _ = start_session(setup)
    edit(session, "main.py", "first\n")
    original = session.git.sha()
    with pytest.raises(PipelineError, match="concrete Korean subject"):
        session.checkpoint_commit(
            "bad",
            original,
            {"main.py": session.data["files"]["main.py"]["hash"]},
            "자동 개발: RUN_ID",
            "본문",
        )
    assert session.git.sha() == original
    assert not session.git.call("diff", "--cached", "--name-only")
    assert (session.root / "main.py").read_text() == "first\n"
