"""Automatic targeting executes real selected tests and retains candidate evidence."""

import copy
import json
import sys

import pytest
from test_workspace import setup as setup
from test_workspace import start_session

from dev_orchestra.application.automatic_tests import model_selection
from dev_orchestra.application.test_selection import select_tests, selected_config, verify_selection
from dev_orchestra.domain.models import Coverage, PipelineError, WorkspaceTestResult
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.test_runner import TestRunner


@pytest.fixture
def candidate(setup):
    svc, rid, repo = setup
    (repo / "tests/test_selected.py").write_text("def test_selected():\n    assert 2 + 2 == 4\n")
    (repo / "tests/test_unrelated.py").write_text("def test_unrelated():\n    assert False\n")
    (repo / "tests/selected.test.mjs").write_text(
        "import {test} from 'node:test';test('selected',()=>{});\n"
    )
    (repo / "tests/browser.spec.ts").write_text("export {};\n")
    (repo / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "node --test tests/*.test.mjs",
                    "browser": "playwright test",
                    "unit": "vitest run",
                }
            }
        )
    )
    git = Git(repo)
    git.commit("테스트 준비")
    config = svc.store.get(rid)["config"]
    config["tests"]["commands"] = {
        "unit": {
            "argv": [sys.executable, "-m", "pytest", "--junitxml=build/results.xml", "tests"],
            "junit_report": "build/results.xml",
        },
        "unrelated": {
            "argv": ["pytest", "tests/test_unrelated.py"],
            "junit_report": "build/other.xml",
        },
        "static": {"kind": "check", "argv": [sys.executable, "-c", "pass"]},
    }
    svc.store.update(
        rid, config=config, test_scope="affected", test_selection_contract=2, base_sha=git.sha()
    )
    return svc, rid, repo


def plan(files=None):
    return [
        {
            "command_id": "unit",
            "files": ["tests/test_selected.py"] if files is None else files,
            "reason": "수정한 기능과 호출부의 회귀 검증",
        }
    ]


def test_no_mapping_selects_only_related_files_and_retains_required_checks(candidate):
    svc, rid, repo = candidate
    run = svc.store.get(rid)
    pending = select_tests(run, repo, run["base_sha"])
    assert pending["included"] == [] and "available_commands" in pending
    with pytest.raises(PipelineError, match="not finalized"):
        verify_selection(run, {"sha": run["base_sha"], "test_selection": pending})
    selection = select_tests(run, repo, run["base_sha"], plan=plan())
    assert selection["included"] == ["static", "unit"]
    assert selection["excluded"] == ["unrelated"]
    assert selection["actual_scope"] == "affected"
    assert selection["resolved_commands"]["unit"]["argv"][-1] == "tests/test_selected.py"
    assert "tests" not in selection["resolved_commands"]["unit"]["argv"]
    assert "tests/test_selected.py" in selection["command_reasons"]["unit"]


@pytest.mark.parametrize(
    "target", ["../secret.py", "/tmp/test.py", "--help", "tests/*.py", "tests/missing.py"]
)
def test_unsafe_or_missing_targets_do_not_fall_back_to_full(candidate, target):
    svc, rid, repo = candidate
    run = svc.store.get(rid)
    with pytest.raises(PipelineError):
        select_tests(run, repo, run["base_sha"], plan=plan([target]))


def test_symlink_target_denied(candidate):
    svc, rid, repo = candidate
    (repo / "tests/link.py").symlink_to("/tmp/outside.py")
    git = Git(repo)
    git.commit("링크 준비")
    with pytest.raises(PipelineError, match="regular file"):
        select_tests(svc.store.get(rid), repo, git.sha(), plan=plan(["tests/link.py"]))


@pytest.mark.parametrize(
    "script,expected",
    [
        ("test", "node"),
        ("browser", "./node_modules/.bin/playwright"),
        ("unit", "./node_modules/.bin/vitest"),
    ],
)
def test_npm_file_adapters_replace_full_targets_preserve_reporters(candidate, script, expected):
    svc, rid, repo = candidate
    run = svc.store.get(rid)
    target = "tests/selected.test.mjs" if script == "test" else "tests/browser.spec.ts"
    argv = ["npm", "run", script]
    if script == "test":
        argv.insert(
            1, "--node-options=--test-reporter=junit --test-reporter-destination=build/results.xml"
        )
    else:
        argv += ["--", "--reporter=junit"]
    run["config"]["tests"]["commands"]["unit"]["argv"] = argv
    selection = select_tests(run, repo, run["base_sha"], plan=plan([target]))
    actual = selection["resolved_commands"]["unit"]["argv"]
    assert actual[0] == expected
    assert "tests/*.test.mjs" not in actual
    assert any("reporter" in arg for arg in actual)
    assert ("tests/browser\\.spec\\.ts$" if script == "browser" else target) == actual[-1]


def test_expansion_requires_reason_and_invalid_plans_cannot_pass(candidate):
    svc, rid, repo = candidate
    run = svc.store.get(rid)
    for invalid in [
        [],
        plan() + plan(),
        [{"command_id": "static", "files": [], "reason": "check only"}],
        [{"command_id": "unit", "files": [], "reason": " "}],
    ]:
        with pytest.raises(PipelineError):
            select_tests(run, repo, run["base_sha"], plan=invalid)
    selection = select_tests(run, repo, run["base_sha"], plan=plan([]))
    assert selection["expanded"] and selection["reasons"][-1] == plan()[0]["reason"]
    with pytest.raises(PipelineError, match="refusing automatic full"):
        model_selection(run, run["base_sha"], [], plan(), input_error="diff unavailable")


def test_required_regression_cannot_be_filtered(candidate):
    svc, rid, repo = candidate
    run = svc.store.get(rid)
    run["config"]["tests"]["affected"] = {"required_commands": ["unit"]}
    with pytest.raises(PipelineError, match="unfiltered"):
        select_tests(run, repo, run["base_sha"], plan=plan())


def test_real_pytest_execution_excludes_failing_unrelated_test_and_checks_evidence(candidate):
    svc, rid, repo = candidate
    session, _ = start_session(candidate, "tester")
    value = session.run_tests(["static", "unit"], test_plan=plan())
    assert value["execution"]["passed"], value["execution"]
    assert value["execution"]["commands"][0]["total"] == 1
    assert value["selection"]["actual_scope"] == "affected"
    assert (
        session.run_tests(["static", "unit"], test_plan=plan())["evidence_id"]
        == value["evidence_id"]
    )
    session.finish(
        WorkspaceTestResult(
            summary="관련 테스트만 실행 확인",
            command_ids=["static", "unit"],
            coverage=[Coverage(criterion_id="AC1", command_ids=["unit"])],
            test_evidence_id=value["evidence_id"],
        )
    )
    payload = session.read_test_payload()
    evidence = {
        "sha": payload["sha"],
        "revision": 0,
        "test_selection": payload["selection"],
        "execution": payload["execution"],
    }
    verify_selection(svc.store.get(rid), evidence)
    forged = copy.deepcopy(evidence)
    forged["execution"]["commands"][0]["argv"] = ["pytest", "tests"]
    with pytest.raises(PipelineError):
        verify_selection(svc.store.get(rid), forged)
    forged = copy.deepcopy(evidence)
    forged["sha"] = "a" * 40
    with pytest.raises(PipelineError):
        verify_selection(svc.store.get(rid), forged)


def test_automatic_selection_reaches_final_approval_without_static_mapping(candidate):
    from test_workflow import FakeAgent
    from test_workspace import edit

    from dev_orchestra.application.plan_approval import binding, decide
    from dev_orchestra.domain.models import AgentResult, TestTarget, WorkspaceResult
    from dev_orchestra.orchestration.runner import Runner

    svc, rid, repo = candidate

    class Agent(FakeAgent):
        def run(self, request, *args):
            if request.role == "tester":
                assert request.context["automatic_test_selection"]
                return AgentResult(
                    summary="관련 회귀 테스트 자동 선택",
                    command_ids=["static", "unit"],
                    test_plan=[TestTarget(**item) for item in plan()],
                    coverage=[Coverage(criterion_id="AC1", command_ids=["unit"])],
                )
            return super().run(request, *args)

        def run_workspace(self, request, directory, session, stop):
            if request.role == "developer":
                edit(session, "main.py", "implemented\n")
                return WorkspaceResult(summary="요구 기능 수정")
            value = session.run_tests(["static", "unit"], test_plan=plan())
            return WorkspaceTestResult(
                summary="관련 회귀 테스트 자동 실행",
                command_ids=value["selection"]["included"],
                test_evidence_id=value["evidence_id"],
                coverage=[Coverage(criterion_id="AC1", command_ids=["unit"])],
            )

    runner = Runner(svc.store, Agent(), TestRunner())
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_plan_approval", svc.store.get(rid).get("error")
    decide(svc, rid, binding(svc.store, svc.store.get(rid))["plan_hash"], True)
    runner.execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert run["test_result"]["test_selection"]["excluded"] == ["unrelated"]
    assert run["test_result"]["execution"]["commands"][0]["total"] == 1
    svc.approve(rid)
    assert svc.store.get(rid)["state"] == "completed"


def test_real_node_adapter_executes_only_selected_file_with_junit(candidate, tmp_path):
    svc, rid, repo = candidate
    (repo / "tests/unrelated.test.mjs").write_text(
        "import {test} from 'node:test';test('unrelated',()=>{throw Error('must not execute')});\n"
    )
    git = Git(repo)
    git.commit("Node 회귀 검증 준비")
    run = svc.store.get(rid)
    run["config"]["tests"]["commands"]["unit"]["argv"] = [
        "npm",
        "--node-options=--test-reporter=junit --test-reporter-destination=build/results.xml",
        "run",
        "test",
    ]
    selection = select_tests(run, repo, git.sha(), plan=plan(["tests/selected.test.mjs"]))
    result = TestRunner().run(repo, selected_config(run, selection), tmp_path / "evidence")
    assert result["passed"], result
    assert result["commands"][0]["total"] == 1


def test_empty_selection_execution_is_not_success(candidate, tmp_path):
    svc, rid, repo = candidate
    (repo / "tests/test_selected.py").write_text("# No executable cases\n")
    git = Git(repo)
    git.commit("테스트 0건 검증 준비")
    run = svc.store.get(rid)
    selection = select_tests(run, repo, git.sha(), plan=plan())
    result = TestRunner().run(repo, selected_config(run, selection), tmp_path / "evidence")
    assert not result["passed"]
    assert result["commands"][0]["status"] == "parse_error"
