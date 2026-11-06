"""Full and affected modes reach the same approval/fix loop using real host JUnit."""

import sys
from pathlib import Path

import pytest
from test_workflow import FakeAgent
from test_workspace import edit
from test_workspace import setup as setup

from dev_orchestra.application.plan_approval import binding, decide
from dev_orchestra.domain.models import (
    Coverage,
    ReviewAssessment,
    ReviewResultV2,
    WorkspaceResult,
    WorkspaceTestResult,
)
from dev_orchestra.infrastructure.test_runner import TestRunner as HostTests
from dev_orchestra.orchestration.runner import Runner


@pytest.mark.parametrize(
    "scope,web",
    [
        ("full", False),
        ("affected", False),
        ("affected", True),
        ("full", "workspace"),
        ("affected", "workspace"),
        ("affected", "fallback"),
        ("full", "test_failure"),
    ],
    ids=[
        "full-cli",
        "affected-cli",
        "affected-web",
        "full-release",
        "affected-release",
        "fallback-release",
        "test-failure-release",
    ],
)
def test_scope_survives_developer_tester_reviewer_fixer_and_final_approval(setup, scope, web):
    svc, rid, repo = setup
    if web:
        from dev_orchestra.infrastructure.migrations import upgrade

        upgrade(svc.store.path, workspace=isinstance(web, str))
        if isinstance(web, str):
            from dev_orchestra.application.service import RunService

            svc = RunService(svc.config)
            rid = svc.create(
                repo, Path(svc.store.get(rid)["directory"]) / "spec.md", test_scope=scope
            )
            assert svc.store.get(rid)["execution_contract"] == "workspace-v2"
        svc.store.update(rid, review_contract=2)
    config = svc.store.get(rid)["config"]
    commands = {}
    for key in ["a", "b"]:
        xml = '<testsuite><testcase name="' + key + '"/></testsuite>'
        script = f"from pathlib import Path;Path('build/{key}.xml').write_text({xml!r})"
        if web == "test_failure" and key == "a":
            script = (
                "from pathlib import Path;import sys;"
                "failed='developer' in Path('main.py').read_text();"
                "Path('build/a.xml').write_text('<testsuite><testcase name=\"a\">'"
                "+ ('<failure message=\"wrong behavior\"/>' if failed else '')"
                "+ '</testcase></testsuite>');sys.exit(1 if failed else 0)"
            )
        commands[key] = {
            "argv": [sys.executable, "-c", script],
            "junit_report": "build/" + key + ".xml",
        }
    commands["static"] = {"argv": [sys.executable, "-c", "pass"], "kind": "check"}
    config["tests"] = {
        "commands": commands,
        "write_paths": ["tests"],
        "output_paths": ["build"],
        "affected": {
            "inputs": {"a": ["main.py"], "b": ["other/**"]},
            "criterion_commands": {"AC1": ["a"]},
            "global_paths": ["shared/**"],
        },
    }
    svc.store.update(rid, config=config, test_scope=scope, test_selection_contract=1)

    class Agent(FakeAgent):
        def run_workspace(self, request, directory, session, stop):
            self.calls.append(request.role)
            if request.role == "tester":
                policy = session.test_policy()
                value = session.run_tests(policy["included"])
                assert value["execution"]["status"] == (
                    "failed" if web == "test_failure" and request.iteration == 0 else "passed"
                )
                assert session.run_tests(policy["included"])["evidence_id"] == value["evidence_id"]
                return WorkspaceTestResult(
                    summary="실제 결과와 검증 범위 확인",
                    command_ids=value["selection"]["included"],
                    test_evidence_id=value["evidence_id"],
                    coverage=[Coverage(criterion_id="AC1", command_ids=["a"])],
                )
            assert request.role in {"developer", "fixer"}
            if request.role == "fixer":
                assert "patch" not in request.context["test_result"]
                assert web == "test_failure" or (
                    request.context["open_issues"]
                    if request.context.get("review_contract") == 2
                    else request.context["review_result"]["issues"]
                )
            edit(session, "main.py", request.role + "\n")
            if web == "fallback" and request.role == "developer":
                edit(session, "unmapped.py", "unmapped input\n")
            return WorkspaceResult(summary="현재 코드에서 요구사항 구현")

        def run(self, request, *args):
            if request.role == "reviewer":
                assert "patch" not in request.context["test_result"]
                assert request.context["diff"]
            result = super().run(request, *args)
            if request.role == "reviewer" and web:
                return ReviewResultV2(
                    **result.model_dump(exclude={"assessments"}),
                    assessments=[
                        ReviewAssessment(
                            issue_id=issue["issue_id"],
                            status="resolved",
                            candidate_sha=request.input_sha,
                            evidence="현재 후보의 수정 코드와 실제 재검증 결과를 확인했습니다",
                        )
                        for issue in request.context.get("open_issues", [])
                    ],
                )
            return result

    class Tests(HostTests):
        def __init__(self):
            self.commands = []

        def run(self, worktree, config, directory, stop=lambda: None):
            self.commands.append(sorted(config.commands))
            return super().run(worktree, config, directory, stop)

    tests, agent = Tests(), Agent(block=0 if web == "test_failure" else 1)
    runner = Runner(svc.store, agent, tests)
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_plan_approval"
    decide(svc, rid, binding(svc.store, svc.store.get(rid))["plan_hash"], True)
    runner.execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls == [
        "planner",
        "developer",
        "tester",
        "reviewer",
        "fixer",
        "tester",
        "reviewer",
    ]
    expected = ["a", "b", "static"] if scope == "full" or web == "fallback" else ["a", "static"]
    assert tests.commands == [expected, expected]
    selection = run["test_result"]["test_selection"]
    assert selection["requested_scope"] == scope
    assert selection["actual_scope"] == ("full" if web == "fallback" else scope)
    if web == "fallback":
        assert selection["expanded"] and selection["reasons"]
    assert run["test_result"]["sha"] == run["review_result"]["sha"] == run["final_sha"]
    svc.approve(rid)
    assert svc.store.get(rid)["state"] == "completed"
    assert (Path(run["directory"]) / "final-report.md").is_file()
    assert (repo / "main.py").read_text() == "original local changes\n"
    from dev_orchestra.application.measurement import measurement

    observed = measurement(svc.store, rid, version_label="candidate", scenario="normal")
    invocations = [
        item for stage in observed["stages"] for item in (stage["test_invocations"] or [])
    ]
    assert len(invocations) == 2  # Repeated run_tests requests reuse the same execution.
    assert [item["status"] for item in invocations] == (
        ["failed", "passed"] if web == "test_failure" else ["passed", "passed"]
    )
