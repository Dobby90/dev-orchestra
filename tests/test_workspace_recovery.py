"""Interrupted role recovery preserves verified files, commits and actual test evidence."""

import json
from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workspace import checkpoint, edit, start_session
from test_workspace import setup as setup

from dev_orchestra.agents.contracts import ResponseError
from dev_orchestra.application.plan_approval import binding, decide
from dev_orchestra.application.workspace_recovery import inspect_workspace
from dev_orchestra.domain.models import (
    Coverage,
    PipelineError,
    StopRequested,
    WorkspaceResult,
    WorkspaceTestResult,
)
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.orchestration.runner import Runner


def planned(setup, agent, tests):
    svc, rid, _ = setup
    runner = Runner(svc.store, agent, tests)
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_plan_approval"
    decide(svc, rid, binding(svc.store, svc.store.get(rid))["plan_hash"], True)
    return runner


class Agent(FakeAgent):
    def run_workspace(self, request, directory, session, stop):
        self.calls.append(request.role)
        if request.role == "tester":
            value = session.run_tests(session.test_policy()["included"])
            return WorkspaceTestResult(
                summary="검증 확인",
                command_ids=value["selection"]["included"],
                test_evidence_id=value["evidence_id"],
                coverage=[Coverage(criterion_id="AC1", command_ids=["unit"])],
            )
        edit(session, request.role + ".py", "implemented\n")
        return WorkspaceResult(summary="남은 구현 완료")


@pytest.mark.parametrize("release_schema", [False, True])
def test_partial_files_and_multiple_checkpoints_resume_same_stage(setup, release_schema):
    if release_schema:
        from dev_orchestra.infrastructure.migrations import upgrade

        upgrade(setup[0].store.path, workspace=True)

    class Interrupted(Agent):
        def run_workspace(self, req, directory, session, stop):
            if req.role == "developer":
                self.calls.append(req.role)
                if self.calls.count(req.role) == 1:
                    edit(session, "main.py", "first\n")
                    self.commit_args = (
                        "one",
                        session.git.sha(),
                        {"main.py": digest(b"first\n")},
                        "기능 구현 one",
                        "독립 작업 단위",
                    )
                    self.first_commit = session.checkpoint_commit(*self.commit_args)
                    edit(session, "second.py", "second\n")
                    checkpoint(session, "two", ["second.py"])
                    edit(session, "pending.py", "preserved\n")
                    raise StopRequested("pause")
                assert req.context["workspace_resume"]["continuing"]
                assert req.input_sha == session.data["input_sha"]
                assert (session.root / "pending.py").read_text() == "preserved\n"
                assert len(session.data["commits"]) == 2
                assert session.checkpoint_commit(*self.commit_args) == self.first_commit
                assert len(session.data["commits"]) == 2
                edit(session, "remaining.py", "remaining\n")
                return WorkspaceResult(summary="보존된 파일에서 남은 작업 완료")
            return super().run_workspace(req, directory, session, stop)

    svc, rid, repo = setup
    agent = Interrupted()
    runner = planned(setup, agent, FakeTests())
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "paused"
    original = svc.store.steps(rid)[-1]
    base = original["input_sha"]
    svc.resume(rid)
    runner.execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    development = [s for s in svc.store.steps(rid) if s["role"] == "developer"]
    assert len(development) == 1 and development[0]["id"] == original["id"]
    assert development[0]["input_sha"] == base and development[0]["status"] == "completed"
    payload = json.loads(Path(development[0]["data"]["artifact"]).read_text())
    assert len(payload["workspace_commits"]) == 3
    assert all(
        name in payload["patch"] for name in ["main.py", "second.py", "pending.py", "remaining.py"]
    )
    assert (repo / "main.py").read_text() == "original local changes\n"
    assert not run.get("workspace_checkpoint")
    svc.approve(rid)


def test_response_repair_locks_edits_without_reimplementing(setup):
    class Malformed(Agent):
        def run_workspace(self, req, directory, session, stop):
            if req.role == "developer":
                self.calls.append(req.role)
                if self.calls.count(req.role) == 1:
                    edit(session, "main.py", "already implemented\n")
                    checkpoint(session, "unit", ["main.py"])
                    raise ResponseError("summary JSON invalid")
                assert req.context["workspace_resume"]["metadata_only"]
                with pytest.raises(PipelineError, match="metadata repair"):
                    edit(session, "main.py", "must not rewrite\n")
                assert session.workspace_state()["checkpoint_count"] == 1
                return WorkspaceResult(summary="이미 구현한 작업 요약 보완")
            return super().run_workspace(req, directory, session, stop)

    svc, rid, _ = setup
    agent = Malformed()
    runner = planned(setup, agent, FakeTests())
    runner.execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls.count("developer") == 2 and run["iteration"] == 0
    step = next(s for s in svc.store.steps(rid) if s["role"] == "developer")
    assert len(json.loads(Path(step["data"]["artifact"]).read_text())["workspace_commits"]) == 1


def test_tester_summary_repair_reuses_execution_and_blocks_replay(setup):
    class Tests(FakeTests):
        calls = 0

        def run(self, *args):
            self.calls += 1
            return super().run(*args)

    class Malformed(Agent):
        def run_workspace(self, req, directory, session, stop):
            if req.role == "tester" and self.calls.count("tester") == 0:
                self.calls.append("tester")
                session.run_tests(["unit"])
                raise ResponseError("invalid test summary")
            if req.role == "tester":
                self.calls.append("tester")
                with pytest.raises(PipelineError, match="reuse"):
                    session.run_tests(["unit"])
                return WorkspaceTestResult(
                    summary="실행 근거를 다시 생성하지 않고 요약 보완",
                    command_ids=["unit"],
                    coverage=[Coverage(criterion_id="AC1", command_ids=["unit"])],
                    test_evidence_id=session.workspace_state()["test_evidence"]["id"],
                )
            return super().run_workspace(req, directory, session, stop)

    svc, rid, _ = setup
    agent, tests = Malformed(), Tests()
    runner = planned(setup, agent, tests)
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval", svc.store.get(rid).get("error")
    assert tests.calls == 1


def test_commit_success_before_ack_recovers_without_model_or_duplicate_commit(setup, monkeypatch):
    svc, rid, _ = setup
    agent = Agent()
    runner = planned(setup, agent, FakeTests())
    real = Git.commit_selected
    lost = []

    def fail(self, *args):
        sha = real(self, *args)
        if not lost:
            lost.append(sha)
            raise PipelineError("commit response lost before journal acknowledgement")
        return sha

    monkeypatch.setattr(Git, "commit_selected", fail)
    runner.execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "needs_input" and run["current_sha"] != lost[0]
    svc.resume(rid)
    runner.execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls.count("developer") == 1
    step = next(s for s in svc.store.steps(rid) if s["role"] == "developer")
    assert step["output_sha"] == lost[0]
    assert (
        Git(Path(run["worktree"])).call("rev-list", "--count", step["input_sha"] + ".." + lost[0])
        == "1"
    )


def test_metadata_budget_survives_changed_sha_and_repeated_resume(setup):
    class Bad(Agent):
        def run_workspace(self, req, directory, session, stop):
            if req.role == "developer":
                self.calls.append("developer")
                if not req.context["workspace_resume"]["metadata_only"]:
                    edit(session, "main.py", "one\n")
                    checkpoint(session, "one", ["main.py"])
                    edit(session, "other.py", "two\n")
                    checkpoint(session, "two", ["other.py"])
                raise ResponseError("metadata invalid")
            return super().run_workspace(req, directory, session, stop)

    svc, rid, _ = setup
    agent = Bad()
    runner = planned(setup, agent, FakeTests())
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "needs_input"
    assert agent.calls.count("developer") == 2
    svc.resume(rid)
    runner.execute(rid)
    assert agent.calls.count("developer") == 2
    assert svc.store.get(rid)["state"] == "needs_input"
    assert svc.store.get(rid)["iteration"] == 0


@pytest.mark.parametrize(
    "change",
    ["file", "ignored", "checksum", "branch", "policy", "ownership", "identity", "unproven_commit"],
)
def test_unverified_recovery_blocks_and_preserves_files(setup, change):
    session, _ = start_session(setup)
    edit(session, "main.py", "preserved\n")
    svc, rid, _ = setup
    if change == "file":
        (session.root / "main.py").write_text("external\n")
    elif change == "ignored":
        (session.root / "ignored.py").write_text("external\n")
    elif change == "checksum":
        ref = svc.store.get(rid)["workspace_checkpoint"]
        (Path(svc.store.get(rid)["directory"]) / ref["snapshot"]).write_text("{}")
    elif change == "branch":
        session.git.call("checkout", "-b", "wrong")
    elif change == "policy":
        config = svc.store.get(rid)["config"]
        config["tests"]["write_paths"] = ["other"]
        svc.store.update(rid, config=config)
    elif change == "ownership":
        svc.store.update(rid, next_role="fixer")
    elif change == "identity":
        session.data["worktree_identity"]["inode"] += 1
        session.save()
    elif change == "unproven_commit":
        session.git.commit("검증되지 않은 외부 커밋")
    with pytest.raises(PipelineError):
        inspect_workspace(svc.store, svc.store.get(rid))
    assert (session.root / "main.py").exists()


def test_unacknowledged_file_write_is_preserved_but_not_adopted(setup, monkeypatch):
    session, _ = start_session(setup)

    def fail():
        raise PipelineError("checkpoint not acknowledged")

    monkeypatch.setattr(session, "save", fail)
    with pytest.raises(PipelineError):
        edit(session, "main.py", "unacknowledged\n")
    with pytest.raises(PipelineError, match="differ"):
        inspect_workspace(setup[0].store, setup[0].store.get(session.run_id))
    assert (session.root / "main.py").read_text() == "unacknowledged\n"


def test_missing_tester_execution_continues_tests_instead_of_metadata_only(setup):
    class Missing(Agent):
        def run_workspace(self, req, directory, session, stop):
            if req.role == "tester" and self.calls.count("tester") == 0:
                self.calls.append("tester")
                raise ResponseError("turn ended before test execution")
            if req.role == "tester":
                assert not req.context["workspace_resume"]["metadata_only"]
            return super().run_workspace(req, directory, session, stop)

    svc, rid, _ = setup
    agent = Missing()
    runner = planned(setup, agent, FakeTests())
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval", svc.store.get(rid).get("error")


def test_fresh_agent_uses_verified_files_when_conversation_is_lost(setup):
    class NoMemory(Agent):
        def run_workspace(self, req, directory, session, stop):
            if req.role == "developer":
                if not req.context["workspace_resume"]["continuing"]:
                    edit(session, "main.py", "preserved partial work\n")
                    raise StopRequested("pause")
                assert "main.py" in req.context["workspace_resume"]["preserved_files"]
                assert (session.root / "main.py").read_text() == "preserved partial work\n"
                edit(session, "remaining.py", "done\n")
                return WorkspaceResult(summary="새 대화에서 검증된 파일을 이어서 완료")
            return super().run_workspace(req, directory, session, stop)

    svc, rid, _ = setup
    runner = planned(setup, NoMemory(), FakeTests())
    runner.execute(rid)
    svc.resume(rid)
    Runner(svc.store, NoMemory(), FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval", svc.store.get(rid).get("error")


def test_mirror_loss_recovers_only_from_acknowledged_immutable_snapshot(setup):
    session, _ = start_session(setup)
    edit(session, "main.py", "preserved\n")
    session.path.write_text("untrusted damaged mirror")
    restored, _ = inspect_workspace(setup[0].store, setup[0].store.get(session.run_id))
    assert restored.data["files"]["main.py"] == session.data["files"]["main.py"]
    assert (session.root / "main.py").read_text() == "preserved\n"


def test_process_ownership_failure_prevents_recovery(setup, monkeypatch):
    from dev_orchestra.application import workspace_recovery

    session, _ = start_session(setup)
    edit(session, "main.py", "preserved\n")

    def fail(*args):
        raise PipelineError("process termination not confirmed")

    monkeypatch.setattr(workspace_recovery, "ensure_stopped", fail)
    with pytest.raises(PipelineError, match="termination"):
        workspace_recovery.recover_workspace(setup[0].store, setup[0].store.get(session.run_id))
    assert (session.root / "main.py").read_text() == "preserved\n"


def test_runner_keeps_changed_checkpoint_branch_in_needs_input(setup):
    session, _ = start_session(setup)
    edit(session, "main.py", "preserved\n")
    session.git.call("checkout", "-b", "externally-changed")
    svc, rid, _ = setup
    Runner(svc.store, Agent(), FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "needs_input"
    assert (session.root / "main.py").read_text() == "preserved\n"


def test_schema5_reopens_same_owned_step_and_clears_finished_time(setup):
    from dev_orchestra.application.workspace_recovery import recover_workspace
    from dev_orchestra.infrastructure.migrations import upgrade

    svc, rid, _ = setup
    upgrade(svc.store.path)
    session, _ = start_session(setup, "fixer")
    edit(session, "main.py", "preserved\n")
    step = session.data["step_id"]
    svc.store.interrupt_steps(rid, "paused")
    svc.store.update(rid, "paused")
    svc.resume(rid)
    recovered = recover_workspace(svc.store, svc.store.get(rid))
    assert recovered.data["step_id"] == step
    with svc.store.connection() as db:
        row = db.execute("SELECT * FROM steps WHERE id=?", (step,)).fetchone()
    assert row["status"] == "running" and row["finished_at"] is None
    assert row["attempt"] == 1 and len(svc.store.steps(rid)) == 1
    assert (session.root / "main.py").read_text() == "preserved\n"


def test_unedited_question_can_follow_existing_replanning_path(setup, tmp_path):
    session, _ = start_session(setup)
    svc, rid, _ = setup
    svc.store.update(rid, "failed")
    answer = tmp_path / "answer.txt"
    answer.write_text("기획 조건을 명확히 합니다")
    svc.resume(rid, answers=answer)
    run = svc.store.get(rid)
    assert run["next_role"] == "planner" and run["revision"] == 1
    assert run["workspace_checkpoint"] is None
    assert (session.root / "main.py").read_text() == "original\n"


def test_changed_requirements_do_not_reuse_partial_stage_permissions(setup, tmp_path):
    session, _ = start_session(setup)
    edit(session, "main.py", "preserved\n")
    svc, rid, _ = setup
    svc.store.update(rid, "failed")
    answer = tmp_path / "answer.txt"
    answer.write_text("구현 요구사항을 변경합니다")
    with pytest.raises(PipelineError, match="requirements"):
        svc.resume(rid, answers=answer)
    assert (session.root / "main.py").read_text() == "preserved\n"
    assert svc.store.get(rid)["next_role"] == "developer"


def test_confirmed_environment_failure_can_retry_with_a_new_tester_attempt(setup):
    class Environment(FakeTests):
        calls = 0

        def run(self, *args):
            self.calls += 1
            result = super().run(*args)
            if self.calls == 1:
                result.update(status="unavailable", passed=False)
                for command in result["commands"]:
                    command.update(status="unavailable", passed=False, error="environment missing")
            return result

    svc, rid, _ = setup
    tests = Environment()
    runner = planned(setup, Agent(), tests)
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "needs_input"
    assert svc.store.get(rid)["workspace_checkpoint"] is None
    svc.resume(rid)
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval", svc.store.get(rid).get("error")
    assert tests.calls == 2
    assert [step["status"] for step in svc.store.steps(rid) if step["role"] == "tester"] == [
        "failed",
        "completed",
    ]


@pytest.mark.parametrize("role", ["tester", "fixer"])
def test_unedited_question_answer_restarts_only_the_trusted_attempt(setup, tmp_path, role):
    from dev_orchestra.infrastructure.migrations import upgrade

    svc, rid, _ = setup
    upgrade(svc.store.path, workspace=True)

    class Asking(Agent):
        asked = False

        def run_workspace(self, req, directory, session, stop):
            if req.role == role and not self.asked:
                self.asked = True
                self.question_step = session.data["step_id"]
                result = WorkspaceTestResult if role == "tester" else WorkspaceResult
                return result(summary="요구 확인", questions=["기존 조건으로 진행할까요?"])
            return super().run_workspace(req, directory, session, stop)

    agent = Asking()
    runner = planned(setup, agent, FakeTests())
    if role == "fixer":
        svc.store.update(rid, next_role="fixer")
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "needs_input"
    # These direct Runner calls bypass Worker's job-finalization wrapper.
    with svc.store.connection() as db:
        db.execute("UPDATE jobs SET state='finished' WHERE run_id=?", (rid,))
    answer = tmp_path / "answer.txt"
    answer.write_text("기존 조건으로 진행하세요")
    svc.resume(rid, answers=answer)
    assert svc.store.get(rid)["workspace_released_steps"] == [agent.question_step]
    runner.execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    steps = [s for s in svc.store.steps(rid) if s["role"] == role]
    assert len(steps) == 2 and steps[-1]["status"] == "completed"
    assert steps[0]["id"] == agent.question_step
