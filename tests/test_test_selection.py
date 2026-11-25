"""Selection is based on cumulative Git inputs and immutable host mappings."""

import copy
import json
import sys
from pathlib import Path

import pytest
from test_workspace import edit, start_session
from test_workspace import setup as setup

from dev_orchestra.agents.context import COVERAGE_BUDGET, execution_summary, role_context
from dev_orchestra.application.test_selection import (
    selection_for_paths,
    verify_selection,
    verify_tool_evidence,
)
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


@pytest.mark.parametrize("container", [True, False], ids=["docker", "file-adapter"])
def test_tool_evidence_validates_junit_and_runtime_adapter_files(tmp_path, container):
    evidence = tmp_path / "test-evidence"
    evidence.mkdir()
    junit = evidence / "unit.junit.xml"
    junit.write_text('<testsuite><testcase name="one"/></testsuite>')
    command = {
        "junit_path": str(junit),
        "junit_hash": digest(junit.read_bytes()),
        "total": 1,
        "failed": 0,
        "skipped": 0,
        "exit_code": 0,
        "passed": True,
        "status": "passed",
    }
    if container:
        command["runtime_adapters"] = ["docker"]
        command["container_environment"] = {"image_id": "sha256:" + "b" * 64}
        checked_file, error = junit, "JUnit evidence checksum mismatch"
    else:
        content = b"// Gradle init script"
        sha = digest(content)
        adapter = evidence / (sha[:16] + "-runtime.gradle")
        adapter.write_bytes(content)
        command["runtime_adapters"] = [{"adapter": "gradle-test-agent", "sha256": sha}]
        checked_file, error = adapter, "runtime adapter checksum mismatch"
    value = {"sha": "a" * 40, "revision": 0, "selection": {}, "execution": {"commands": [command]}}
    raw = json.dumps(value).encode()
    (evidence / "evidence.json").write_bytes(raw)
    step = {"data": {"artifact": str(tmp_path / "result.json")}}
    payload = {
        "sha": value["sha"],
        "revision": value["revision"],
        "test_selection": value["selection"],
        "execution": value["execution"],
        "test_evidence_reference": {"id": evidence.name, "hash": digest(raw)},
    }

    verify_tool_evidence({}, step, payload)

    checked_file.write_text("changed after execution")
    with pytest.raises(PipelineError, match=error):
        verify_tool_evidence({}, step, payload)


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
                "coverage": [{"criterion_id": "AC1", "command_ids": ["test"]}],
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
    assert request.context["test_result"].get("coverage") == [
        {"criterion_id": "AC1", "command_ids": ["test"]}
    ]
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


@pytest.mark.parametrize("role", ["developer", "fixer", "tester"])
def test_workspace_state_returns_only_requested_hashes(setup, role):
    session, request = start_session(setup, role)
    full = session.workspace_state()
    paths = ["tests/test_main.py", "tests/new_test.py"]
    response = dispatch(
        session,
        {
            "method": "tools/call",
            "params": {"name": "workspace_state", "arguments": {"paths": paths}},
        },
    )
    assert not response.get("isError")
    assert json.loads(response["content"][0]["text"]) == {
        "candidate_sha": request.input_sha,
        "files": {paths[0]: full["files"][paths[0]]["hash"], paths[1]: None},
    }
    summary = session.workspace_state(summary_only=True)
    assert summary == {key: value for key, value in full.items() if key != "files"}
    assert full["files"]["main.py"]["hash"] == digest(b"original\n")
    with pytest.raises(PipelineError, match="cannot be combined"):
        session.workspace_state(paths, summary_only=True)
    for name in ("../outside", ".git/config", "build/results.xml", ".env", "node_modules/index.js"):
        with pytest.raises(PipelineError):
            session.workspace_state([name])
    import os

    outside_lookup = session.root / "main.py"
    original = outside_lookup.stat()
    outside_lookup.write_bytes(b"external\n")
    os.utime(outside_lookup, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert outside_lookup.stat().st_size == original.st_size
    with pytest.raises(PipelineError, match="unrecognized workspace changes"):
        session.workspace_state(paths)
    with pytest.raises(PipelineError, match="unrecognized workspace changes"):
        session.workspace_state(summary_only=True)


def test_tester_revalidates_evidence_at_each_boundary_with_single_reads(setup, monkeypatch):
    from collections import Counter

    from test_workflow import FakeTests

    class Tests(FakeTests):
        def run(self, worktree, config, directory, stop):
            value = super().run(worktree, config, directory, stop)
            directory.mkdir()
            junit = directory / "unit.junit.xml"
            raw = b'<testsuite><testcase name="one"/></testsuite>'
            junit.write_bytes(raw)
            value["commands"][0].update(
                junit_path=str(junit), junit_hash=digest(raw), failed=0, skipped=0
            )
            return value

    session, _ = start_session(setup, "tester")
    session._test_runner = Tests()
    reads = Counter()
    original_open = Path.open

    def count_open(path, mode="r", *args, **kwargs):
        if mode.startswith("r") and path.name in {"evidence.json", "unit.junit.xml"}:
            reads[path.name] += 1
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", count_open)
    expected = {"evidence.json": 1, "unit.junit.xml": 1}
    value = session.run_tests(["unit"])
    assert reads == expected
    reads.clear()
    response = WorkspaceTestResult(
        summary="실행 근거 확인", command_ids=["unit"], test_evidence_id=value["evidence_id"]
    )
    session.finish(response)
    assert reads == expected
    reads.clear()
    session.load_test_result(response)
    assert reads == expected

    directory = session.path.parent / value["evidence_id"]
    junit = directory / "unit.junit.xml"
    raw = junit.read_bytes()
    junit.write_bytes(raw.replace(b"one", b"two"))
    with pytest.raises(PipelineError, match="JUnit evidence checksum mismatch"):
        session.load_test_result(response)
    junit.write_bytes(raw)
    evidence = directory / "evidence.json"
    raw = evidence.read_bytes()
    evidence.write_bytes(raw + b" ")
    with pytest.raises(PipelineError, match="test tool evidence checksum mismatch"):
        session.load_test_result(response)
    evidence.write_bytes(raw)
    session.git.call("commit", "--allow-empty", "-m", "외부 변경")
    with pytest.raises(PipelineError, match="HEAD changed"):
        session.load_test_result(response)


@pytest.mark.parametrize("kind", ["passed", "failed", "parse_error", "timeout"])
def test_tester_observes_real_host_outcomes_and_reuses_same_candidate(setup, kind):
    svc, rid, _ = setup
    run = svc.store.get(rid)
    config = copy.deepcopy(run["config"])
    xml = (
        '<testsuite><testcase name="one">'
        + (
            '<failure message="expected 2; actual 1">assertion details</failure>'
            if kind == "failed"
            else ""
        )
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
    command = session.read_test_evidence(value["evidence_id"], command_id="unit")
    payload = session.read_test_payload()
    assert (command["sha"], command["revision"]) == (value["sha"], payload["revision"])
    assert command["command"]["status"] == kind
    assert command["command"]["junit_hash"] == payload["execution"]["commands"][0].get("junit_hash")
    with pytest.raises(PipelineError, match="command was not executed"):
        session.read_test_evidence(value["evidence_id"], command_id="other")
    with pytest.raises(PipelineError):
        session.read_test_evidence("another-run")
    if kind == "failed":
        raw_command = payload["execution"]["commands"][0]
        assert json.loads(command["text"]) == {
            key: raw_command[key] for key in ("error", "failures", "failure_details")
        }
        assert (
            json.loads(command["text"])["failure_details"][0]["message"] == "expected 2; actual 1"
        )
        first = session.read_test_evidence(value["evidence_id"], limit=48, command_id="unit")
        later = session.read_test_evidence(value["evidence_id"], offset=48, command_id="unit")
        assert first["truncated"] and first["text"] + later["text"] == command["text"]
        Path(raw_command["junit_path"]).write_text("tampered")
        with pytest.raises(PipelineError, match="JUnit evidence checksum mismatch"):
            session.read_test_evidence(value["evidence_id"], command_id="unit")
    if kind == "passed":
        edit(session, "tests/new_test.py", "assert True\n")
        with pytest.raises(PipelineError, match="stale"):
            session.read_test_evidence(value["evidence_id"], command_id="unit")
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


def test_command_evidence_view_omits_other_command_data(setup):
    from test_workflow import FakeTests

    svc, rid, _ = setup
    config = svc.store.get(rid)["config"]
    config["tests"]["commands"]["check"] = {"kind": "check", "argv": ["other_command"]}
    svc.store.update(rid, config=config)
    session, _ = start_session(setup, "tester")
    session._test_runner = FakeTests()
    result = session.run_tests(["unit", "check"])
    full = session.read_test_evidence(result["evidence_id"])
    targeted = session.read_test_evidence(result["evidence_id"], command_id="unit")
    assert "other_command" in full["text"]
    assert "other_command" not in targeted["text"] and len(json.dumps(targeted)) < len(full["text"])
    assert targeted["command"]["command_id"] == "unit"


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
        "coverage": [{"criterion_id": f"AC{i}", "command_ids": ["unit"]} for i in range(1, 201)],
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
    evidence_id = reviewer.data["evidence_id"]
    page = reviewer.read_test_evidence(evidence_id)
    assert page["truncated"]
    text = page["text"]
    while page["truncated"]:
        page = reviewer.read_test_evidence(evidence_id, offset=len(text))
        text += page["text"]
    assert "must not resend source patch" not in text
    evidence = json.loads(text)
    assert evidence["execution"] == payload["execution"]
    assert evidence["coverage"] == payload["coverage"]
    assert (evidence["sha"], evidence["revision"]) == (value["sha"], 0)

    request = AgentRequest(
        run_id=rid,
        role="reviewer",
        worktree=str(session.root),
        input_sha=value["sha"],
        iteration=0,
        context={"test_result": payload, "revision": 0},
    )
    role_context(request)
    tests = request.context["test_result"]
    assert 0 < tests["omitted_coverage"] < len(payload["coverage"])
    assert len(json.dumps(tests["coverage"], ensure_ascii=False).encode()) <= COVERAGE_BUDGET
    assert tests["coverage"] == evidence["coverage"][: len(tests["coverage"])]

    for stale in ({"sha": "0" * 40}, {"revision": 1}):
        current = request.model_copy(update={"context": {"test_result": payload | stale}})
        role_context(current)
        assert "test_result" not in current.context
    filtered = dispatch(
        reviewer,
        {
            "method": "tools/call",
            "params": {
                "name": "read_test_evidence",
                "arguments": {"evidence_id": reviewer.data["evidence_id"], "command_id": "unit"},
            },
        },
    )
    assert filtered["isError"]
    with pytest.raises(PipelineError):
        reviewer.read_test_evidence("another-run")
    svc.store.update(rid, revision=1)
    with pytest.raises(PipelineError, match="ownership denied"):
        reviewer.read_test_evidence(evidence_id)
    svc.store.update(rid, revision=0)
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


@pytest.mark.parametrize(
    "version,mapping,covered,valid",
    [
        (8, ["integration"], ["a"], False),
        (8, ["integration", "a"], ["a"], True),
        (8, ["types"], ["a"], True),
        (8, ["types"], ["types"], False),
        (8, [], ["a"], True),
        (7, ["integration"], ["a"], True),
    ],
    ids=["wrong-test", "alternative", "build-and-test", "build-only", "unmapped", "v7"],
)
def test_automatic_criterion_mapping_in_response_and_stored_result(
    mapped, setup, version, mapping, covered, valid
):
    from dev_orchestra.agents.contracts import ResponseError
    from dev_orchestra.config import Config
    from dev_orchestra.domain.models import AgentResult
    from dev_orchestra.orchestration.runner import Runner

    svc, rid, repo = setup
    sha = svc.store.get(rid)["base_sha"]
    mapped.update(repo=str(repo), base_sha=sha, test_selection_contract=version)
    mapped["config"]["tests"]["affected"]["criterion_commands"] = (
        {"AC1": mapping} if mapping else {}
    )
    selection = selection_for_paths(
        mapped, sha, ["src/a/model.py"], plan=[{"command_id": "a", "reason": "coverage"}]
    )
    assert "integration" in selection["included"]  # A configured dependent of a.
    config = Config.model_validate(mapped["config"])
    result = AgentResult(
        summary="tests",
        command_ids=selection["included"],
        coverage=[Coverage(criterion_id="AC1", command_ids=covered)],
    )
    payload = result.model_dump() | {
        "sha": sha,
        "test_selection": selection,
        "execution": {
            "commands": [
                dict(command_id=key, **command)
                for key, command in selection["resolved_commands"].items()
            ]
        },
    }
    if valid:
        Runner.validate_test_result(mapped, result, config, selection)
        verify_selection(mapped, payload)
    else:
        with pytest.raises(ResponseError, match="coverage"):
            Runner.validate_test_result(mapped, result, config, selection)
        with pytest.raises(PipelineError, match="coverage"):
            verify_selection(mapped, payload)


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


def test_reported_command_inventory_must_match_host_selection(mapped):
    from dev_orchestra.config import Config
    from dev_orchestra.domain.models import AgentResult
    from dev_orchestra.orchestration.runner import Runner

    selection = selection_for_paths(mapped, "a" * 40, ["src/a/model.py"])
    result = AgentResult(
        summary="legacy",
        command_ids=selection["included"],
        coverage=[Coverage(criterion_id="AC1", command_ids=["a"])],
    )
    Runner.validate_test_result(mapped, result, Config.model_validate(mapped["config"]), selection)
    assert "b" not in result.command_ids and "b" not in selection["included"]
    result.coverage = [Coverage(criterion_id="AC1", command_ids=["b"])]
    with pytest.raises(PipelineError):
        Runner.validate_test_result(
            mapped, result, Config.model_validate(mapped["config"]), selection
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
