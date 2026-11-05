"""Selection is based on cumulative Git inputs and immutable host mappings."""

import copy
import json
import sys
from pathlib import Path

import pytest
from test_workspace import edit, start_session
from test_workspace import setup as setup

from dev_orchestra.agents.context import execution_summary, role_context
from dev_orchestra.application.test_selection import selection_for_paths, verify_selection
from dev_orchestra.domain.models import (
    AgentRequest,
    Coverage,
    PipelineError,
    WorkspaceResult,
    WorkspaceTestResult,
)
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.workspace_mcp import dispatch


@pytest.fixture
def mapped():
    commands = {
        name: {"argv": ["pytest", name], "junit_report": "build/" + name + ".xml"}
        for name in ["a", "b", "integration", "regression"]
    }
    commands["types"] = {"argv": ["typecheck"], "kind": "check"}
    return {
        "config": {
            "tests": {
                "commands": commands,
                "affected": {
                    "inputs": {"a": ["src/a/**"], "b": ["src/b/**"]},
                    "dependencies": {"integration": ["a"]},
                    "required_commands": ["regression"],
                    "criterion_commands": {"AC1": ["a", "b"]},
                    "global_paths": ["shared/**", "*lock*"],
                },
            }
        },
        "test_scope": "affected",
        "test_selection_contract": 1,
        "base_sha": "b" * 40,
        "criteria": [{"id": "AC1", "description": "behavior"}],
        "revision": 0,
    }


def test_direct_indirect_and_mandatory_commands_selected(mapped):
    result = selection_for_paths(mapped, "a" * 40, ["src/a/model.py"])
    assert result["included"] == ["a", "integration", "regression", "types"]
    assert result["excluded"] == ["b"] and result["actual_scope"] == "affected"
    assert not result["expanded"]


@pytest.mark.parametrize("case", ["shared", "missing", "zero", "criteria", "unsupported"])
def test_uncertain_inputs_expand_to_all(mapped, case):
    paths = ["src/a/model.py"]
    if case == "shared":
        paths = ["shared/core.py"]
    elif case == "missing":
        paths = ["unknown.py"]
    elif case == "zero":
        paths = []
    elif case == "criteria":
        mapped["criteria"].append({"id": "AC2", "description": "unmapped"})
    elif case == "unsupported":
        mapped["config"]["tests"].pop("affected")
    result = selection_for_paths(mapped, "a" * 40, paths)
    assert result["actual_scope"] == "full" and result["expanded"] and result["reasons"]
    assert not result["excluded"]


def test_missing_scope_uses_full_and_does_not_require_new_evidence(mapped):
    mapped.pop("test_scope")
    mapped.pop("test_selection_contract")
    result = selection_for_paths(mapped, "a" * 40, ["src/a/model.py"])
    assert result["requested_scope"] == result["actual_scope"] == "full"
    verify_selection(mapped, {"sha": "a" * 40})


@pytest.mark.parametrize("mutation", ["sha", "revision", "mode", "policy", "commands", "omitted"])
def test_other_candidate_or_policy_cannot_reuse_success(mapped, mutation):
    selection = selection_for_paths(mapped, "a" * 40, ["src/a/model.py"])
    payload = {
        "sha": "a" * 40,
        "revision": 0,
        "test_selection": selection,
        "execution": {
            "commands": [
                dict(command_id=key, **mapped["config"]["tests"]["commands"][key])
                for key in selection["included"]
            ]
        },
    }
    verify_selection(mapped, payload)
    if mutation == "sha":
        payload["sha"] = "c" * 40
    elif mutation == "revision":
        mapped["revision"] = 1
    elif mutation == "mode":
        mapped["test_scope"] = "full"
    elif mutation == "policy":
        mapped["config"]["tests"]["affected"]["required_commands"].append("b")
    elif mutation == "commands":
        payload["execution"]["commands"][0]["argv"] = ["wrong"]
    elif mutation == "omitted":
        payload["execution"]["commands"].pop()
    with pytest.raises(PipelineError):
        verify_selection(mapped, payload)


def test_role_inputs_do_not_repeat_applied_patches_and_keep_review_obligations():
    marker = "already applied patch" * 5000
    request = AgentRequest(
        run_id="r",
        role="reviewer",
        worktree="/tmp/repo",
        iteration=1,
        input_sha="a" * 40,
        context={
            "diff": "cumulative diff",
            "test_result": {
                "patch": marker,
                "sha": "a" * 40,
                "execution": {
                    "commands": [
                        {
                            "command_id": "test",
                            "status": "failed",
                            "failure_details": ["failure" * 10000],
                        }
                    ]
                },
            },
            "review_result": {"patch": marker, "issues": [{"id": "unresolved"}]},
            "test_change_records": [{"patch": marker, "changes": [{"file": "tests/test_a.py"}]}],
            "open_issues": [{"issue_id": "old-issue"}],
        },
    )
    role_context(request)
    value = json.dumps(request.context)
    assert marker not in value and len(value) < 25000
    assert request.context["diff"] == "cumulative diff"
    assert request.context["open_issues"] == [{"issue_id": "old-issue"}]
    assert (
        "callers" in request.context["review_scope"]
        and "last Fixer diff" in request.context["review_scope"]
    )
    assert request.context["test_result"]["execution"]["truncated"]
    assert (
        len(
            json.dumps(
                execution_summary(
                    {
                        "commands": [
                            {"command_id": str(i), "failure_details": ["x" * 10000]}
                            for i in range(300)
                        ]
                    }
                )
            )
        )
        < 22000
    )


def test_tester_cannot_edit_product_or_invoke_commit(setup):
    session, _ = start_session(setup, "tester")
    with pytest.raises(PipelineError, match="production"):
        edit(session, "main.py", "bad\n")
    assert {tool["name"] for tool in dispatch(session, {"method": "tools/list"})["tools"]} == {
        "edit_file",
        "workspace_state",
        "run_tests",
        "test_policy",
        "read_test_evidence",
    }
    with pytest.raises(PipelineError, match="unsupported"):
        dispatch(session, {"method": "tools/call", "params": {"name": "checkpoint_commit"}})


@pytest.mark.parametrize("kind", ["passed", "failed", "parse_error", "timeout"])
def test_tester_observes_real_host_outcomes_and_reuses_same_candidate(setup, kind):
    svc, rid, _ = setup
    run = svc.store.get(rid)
    config = copy.deepcopy(run["config"])
    xml = (
        '<testsuite><testcase name="one">'
        + ("<failure>actual failure</failure>" if kind == "failed" else "")
        + "</testcase></testsuite>"
    )
    script = (
        "import time;time.sleep(5)"
        if kind == "timeout"
        else "pass"
        if kind == "parse_error"
        else f"from pathlib import Path;Path('build/results.xml').write_text({xml!r})"
    )
    config["tests"]["commands"]["unit"].update(
        argv=[sys.executable, "-c", script], timeout_seconds=1
    )
    svc.store.update(rid, config=config)
    session, _ = start_session(setup, "tester")
    value = session.run_tests(["unit"])
    assert value["execution"]["status"] == kind
    assert value["execution"]["passed"] == (kind == "passed")
    assert session.run_tests(["unit"])["evidence_id"] == value["evidence_id"]
    response = WorkspaceTestResult(
        summary="실제 결과 확인",
        command_ids=["unit"],
        coverage=[Coverage(criterion_id="AC1", command_ids=["unit"])],
        test_evidence_id=value["evidence_id"],
    )
    session.finish(response)
    assert session.read_test_evidence(value["evidence_id"], limit=100)["truncated"]
    with pytest.raises(PipelineError):
        session.read_test_evidence("another-run")
    if kind == "passed":
        edit(session, "tests/new_test.py", "assert True\n")
        with pytest.raises(PipelineError, match="stale"):
            session.finish(response)
        value2 = session.run_tests(["unit"])
        assert value2["sha"] != value["sha"] and value2["evidence_id"] != value["evidence_id"]
        assert session.data["commits"][0]["files"] == {
            "tests/new_test.py": digest(b"assert True\n")
        }


def test_missing_host_run_is_not_model_success(setup):
    session, _ = start_session(setup, "tester")
    with pytest.raises(PipelineError, match="must run host"):
        session.finish(
            WorkspaceTestResult(summary="passed", command_ids=["unit"], test_evidence_id="invented")
        )
    with pytest.raises(PipelineError, match="required command"):
        session.run_tests([])


def test_test_edit_lock_and_summary_error_keep_valid_execution(setup):
    from test_workflow import FakeTests

    session, _ = start_session(setup, "tester")

    class CheckLock(FakeTests):
        calls = 0

        def run(self, *args):
            self.calls += 1
            with pytest.raises(PipelineError, match="locked"):
                edit(session, "tests/race.py", "bad\n")
            return super().run(*args)

    session._test_runner = CheckLock()
    value = session.run_tests(["unit"])
    with pytest.raises(PipelineError, match="reference"):
        session.finish(
            WorkspaceTestResult(summary="wrong summary metadata", test_evidence_id="wrong")
        )
    assert session.run_tests(["unit"])["evidence_id"] == value["evidence_id"]
    assert session._test_runner.calls == 1
    assert session.read_test_payload()["execution"]["passed"]


def test_selection_failure_expands_to_full(mapped, monkeypatch):
    from dev_orchestra.application.test_selection import select_tests
    from dev_orchestra.infrastructure.git import Git

    def fail(*args, **kwargs):
        raise PipelineError("unavailable")

    monkeypatch.setattr(Git, "call", fail)
    result = select_tests(mapped, Path("/tmp"), "a" * 40)
    assert result["actual_scope"] == "full" and result["input_error"] and result["expanded"]


def test_readonly_review_evidence_keeps_unresolved_issues_and_diagnostics(setup):
    from test_workflow import FakeTests

    from dev_orchestra.infrastructure.files import write
    from dev_orchestra.infrastructure.workspace import ReviewEvidenceSession

    session, _ = start_session(setup, "tester")
    session._test_runner = FakeTests()
    value = session.run_tests(["unit"])
    payload = {
        "sha": value["sha"],
        "revision": 0,
        "patch": "must not resend source patch",
        "execution": session.read_test_payload()["execution"],
        "test_selection": value["selection"],
    }
    artifact = session.path.parent / "result.json"
    checksum = write(artifact, payload)
    svc, rid, _ = setup
    svc.store.finish_step(
        session.data["step_id"], value["sha"], {"artifact": str(artifact), "hash": checksum}
    )
    svc.store.update(rid, "reviewing", next_role="reviewer")
    step_id, _ = svc.store.start_step(rid, "reviewer", 0, value["sha"])
    reviewer = ReviewEvidenceSession.create(
        svc.store, svc.store.get(rid), step_id, session.path.parent / "review"
    )
    assert [tool["name"] for tool in dispatch(reviewer, {"method": "tools/list"})["tools"]] == [
        "read_test_evidence"
    ]
    evidence = reviewer.read_test_evidence(reviewer.data["evidence_id"])
    assert (
        "must not resend source patch" not in evidence["text"] and "execution" in evidence["text"]
    )
    with pytest.raises(PipelineError):
        reviewer.read_test_evidence("another-run")
    svc.store.finish_step(step_id, value["sha"], {})
    with pytest.raises(PipelineError):
        reviewer.read_test_evidence(reviewer.data["evidence_id"])


def test_repair_budget_can_increase_without_changing_execution_permissions(setup):
    from dev_orchestra.infrastructure.evidence import verify_workspace_commits

    session, _ = start_session(setup)
    edit(session, "main.py", "new\n")
    from test_workspace import checkpoint

    checkpoint(session, "unit", ["main.py"])
    result, sha, commits = session.finish(WorkspaceResult(summary="완료"))
    svc, rid, _ = setup
    run = svc.store.get(rid)
    config = copy.deepcopy(run["config"])
    config["workflow"]["max_fix_attempts"] += 1
    svc.store.update(rid, config=config)
    session.guard()
    verify_workspace_commits(
        svc.store.get(rid),
        {
            "role": "developer",
            "input_sha": session.data["input_sha"],
            "output_sha": sha,
            "iteration": 0,
            "id": session.data["step_id"],
        },
        {"workspace_commits": commits, "revision": 0},
    )
    config["tests"]["commands"]["unit"]["argv"] = ["not-allowed"]
    svc.store.update(rid, config=config)
    with pytest.raises(PipelineError):
        session.guard()


def test_host_unavailable_is_not_a_test_failure_or_success(setup, monkeypatch):
    import dev_orchestra.infrastructure.test_runner as tests

    def unavailable(*args, **kwargs):
        raise PipelineError("runtime unavailable")

    monkeypatch.setattr(tests, "sandbox_command", unavailable)
    session, _ = start_session(setup, "tester")
    value = session.run_tests(["unit"])
    assert value["execution"]["status"] == "unavailable"
    assert not value["execution"]["passed"]


def test_coverage_cannot_replace_host_criterion_mapping_with_unrelated_regression(mapped):
    from dev_orchestra.config import Config
    from dev_orchestra.domain.models import AgentResult
    from dev_orchestra.orchestration.runner import Runner

    selection = selection_for_paths(mapped, "a" * 40, ["src/a/main.py"])
    result = AgentResult(
        summary="tests",
        command_ids=selection["included"],
        coverage=[Coverage(criterion_id="AC1", command_ids=["regression"])],
    )
    with pytest.raises(PipelineError, match="criterion-to-test"):
        Runner.validate_test_result(
            mapped, result, Config.model_validate(mapped["config"]), selection
        )
    result.coverage = [Coverage(criterion_id="AC1", command_ids=["a"])]
    Runner.validate_test_result(mapped, result, Config.model_validate(mapped["config"]), selection)


def test_test_runner_setup_error_is_retained_as_unavailable(setup):
    session, _ = start_session(setup, "tester")

    class MissingEnvironment:
        def run(self, *args):
            raise PipelineError("test environment unavailable")

    session._test_runner = MissingEnvironment()
    value = session.run_tests(["unit"])
    assert value["execution"]["status"] == "unavailable" and not value["execution"]["passed"]
    assert session.read_test_payload()["execution"]["commands"][0]["error"]


def test_git_selection_uses_cumulative_changes_across_checkpoints(setup, mapped):
    from test_workspace import checkpoint

    from dev_orchestra.application.test_selection import select_tests

    svc, rid, _ = setup
    run = svc.store.get(rid)
    config = copy.deepcopy(run["config"])
    config["tests"] = mapped["config"]["tests"]
    config["tests"]["affected"]["inputs"]["a"] = ["main.py"]
    svc.store.update(rid, config=config, test_scope="affected")
    session, _ = start_session(setup)
    edit(session, "main.py", "first checkpoint\n")
    first = checkpoint(session, "first", ["main.py"])
    edit(session, "src/b/module.py", "second checkpoint\n")
    checkpoint(session, "second", ["src/b/module.py"])
    result = select_tests(svc.store.get(rid), session.root, session.git.sha())
    assert result["base_sha"] == run["base_sha"] and result["base_sha"] != first["sha"]
    assert result["changed_files"] == ["main.py", "src/b/module.py"]
    assert set(result["included"]) == set(config["tests"]["commands"])


def test_legacy_affected_metadata_inventory_cannot_choose_execution(mapped):
    from dev_orchestra.config import Config
    from dev_orchestra.domain.models import AgentResult
    from dev_orchestra.orchestration.runner import Runner

    selection = selection_for_paths(mapped, "a" * 40, ["src/a/model.py"])
    result = AgentResult(
        summary="legacy",
        command_ids=list(mapped["config"]["tests"]["commands"]),
        coverage=[Coverage(criterion_id="AC1", command_ids=["a"])],
    )
    Runner.validate_test_result(
        mapped, result, Config.model_validate(mapped["config"]), selection, legacy_scope=True
    )
    assert "b" in result.command_ids and "b" not in selection["included"]
    result.coverage = [Coverage(criterion_id="AC1", command_ids=["b"])]
    with pytest.raises(PipelineError):
        Runner.validate_test_result(
            mapped, result, Config.model_validate(mapped["config"]), selection, legacy_scope=True
        )


def test_selected_commands_preserve_existing_policy_execution_order(mapped):
    from dev_orchestra.application.test_selection import selected_config

    commands = mapped["config"]["tests"]["commands"]
    mapped["config"]["tests"]["commands"] = {key: commands[key] for key in reversed(commands)}
    selection = selection_for_paths(mapped, "a" * 40, ["src/a/model.py"])
    expected = [
        key for key in mapped["config"]["tests"]["commands"] if key in selection["included"]
    ]
    assert list(selected_config(mapped, selection).commands) == expected


def test_legacy_session_without_policy_version_can_use_checkpoint_tool(setup):
    from test_workspace import checkpoint

    from dev_orchestra.application.test_changes import checksum

    session, _ = start_session(setup)
    session.data.pop("policy_hash_version")
    session.data["policy_hash"] = checksum(setup[0].store.get(session.run_id)["config"])
    session.save()
    edit(session, "main.py", "legacy session\n")
    result = checkpoint(session, "legacy", ["main.py"])
    assert result["parent"] == session.data["input_sha"]
    assert session.data["commits"][0]["policy_hash_version"] == 1
