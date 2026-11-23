"""Automatic targeting executes real selected tests and retains candidate evidence."""

import copy
import json
import sys

import pytest
from test_workspace import setup as setup
from test_workspace import start_session

from dev_orchestra.application.automatic_tests import (
    AUTOMATIC_TEST_CONTRACT,
    deferred_builds,
    model_selection,
)
from dev_orchestra.application.test_selection import select_tests, selected_config, verify_selection
from dev_orchestra.application.validation import run_test_policy
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
    "manager,script,expected",
    [
        ("npm", "test", "node"),
        ("npm", "browser", "./node_modules/.bin/playwright"),
        ("npm", "unit", "./node_modules/.bin/vitest"),
        ("pnpm", "browser", "./node_modules/.bin/playwright"),
        ("pnpm", "unit", "./node_modules/.bin/vitest"),
    ],
)
def test_package_file_adapters_replace_full_targets_preserve_reporters(
    candidate, manager, script, expected
):
    svc, rid, repo = candidate
    run = svc.store.get(rid)
    target = "tests/selected.test.mjs" if script == "test" else "tests/browser.spec.ts"
    argv = [manager, "run", script]
    if script == "test":
        argv.insert(
            1, "--node-options=--test-reporter=junit --test-reporter-destination=build/results.xml"
        )
    else:
        if manager == "npm":
            argv += ["--"]
        argv += ["--reporter=junit"]
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


@pytest.mark.parametrize("manager", ["yarn", "npm"])
def test_jest_exact_file_selection_preserves_package_invocation_and_evidence(candidate, manager):
    svc, rid, repo = candidate
    git = Git(repo)
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest --silent"}}))
    git.commit("Jest 파일 선택 준비")
    run = svc.store.get(rid)
    run["test_selection_contract"] = AUTOMATIC_TEST_CONTRACT
    command = run["config"]["tests"]["commands"]["unit"]
    extra = ["--ci", "--runInBand", "--json", "--outputFile=build/test-jest.json"]
    separator = ["--"] if manager == "npm" else []
    command["argv"] = [manager, "run", "test", *separator, *extra]
    target = plan(["tests/selected.test.mjs"])
    result = select_tests(run, repo, git.sha(), plan=target)
    assert selected_config(run, result).commands["unit"].argv == [
        *command["argv"],
        "--runTestsByPath",
        "./tests/selected.test.mjs",
    ]
    run["test_selection_contract"] = 3
    with pytest.raises(PipelineError, match="no safe file adapter"):
        select_tests(run, repo, git.sha(), plan=target)
    run["test_selection_contract"] = AUTOMATIC_TEST_CONTRACT
    command["argv"].append("--watch")
    with pytest.raises(PipelineError, match="unsupported Jest option"):
        select_tests(run, repo, git.sha(), plan=target)


def test_jest_keeps_frontend_builds_scoped_to_affected_packages(candidate):
    svc, rid, repo = candidate
    manifests = {
        ".": {"scripts": {"test": "jest", "build": "vue-cli-service build"}},
        "manual": {
            "scripts": {"build": "docusaurus build"},
            "dependencies": {"@docusaurus/core": "2.4.3"},
        },
        "portal": {"scripts": {"build": "vite build"}, "devDependencies": {"vite": "5.0.0"}},
        "studio": {
            "scripts": {
                "build": "vue-cli-service build --skip-plugins @vue/cli-plugin-eslint --mode=demo"
            },
            "devDependencies": {"@vue/cli-service": "5.0.1"},
        },
    }
    for directory, manifest in manifests.items():
        root = repo / directory
        root.mkdir(exist_ok=True)
        (root / "package.json").write_text(json.dumps(manifest))
        (root / "yarn.lock").write_text("# yarn lockfile v1\n")
    git = Git(repo)
    git.commit("독립 패키지 빌드 준비")
    sha = git.sha()
    run = svc.store.get(rid)
    run.update(base_sha=sha, test_selection_contract=AUTOMATIC_TEST_CONTRACT)
    tests = run["config"]["tests"]
    tests["commands"]["unit"]["argv"] = [
        "yarn",
        "run",
        "test",
        "--ci",
        "--json",
        "--outputFile=build/test-jest.json",
    ]
    for name, directory in [
        ("app", "."),
        ("manual", "manual"),
        ("portal", "portal"),
        ("studio", "studio"),
    ]:
        tests["commands"][name] = {
            "kind": "check",
            "argv": ["yarn", "run", "build"],
            "working_directory": directory,
        }
    check_plan = plan(["tests/selected.test.mjs"])
    app_change = ["src/AlarmAddPopup.vue"]
    assert deferred_builds(run, sha, run_test_policy(run)) == {"manual", "portal", "studio"}
    selection = model_selection(run, sha, app_change, check_plan)
    assert selection["included"] == ["app", "static", "unit"]
    assert selection["excluded"] == ["manual", "portal", "studio", "unrelated"]
    assert selection["resolved_commands"]["unit"]["argv"][-2:] == [
        "--runTestsByPath",
        "./tests/selected.test.mjs",
    ]
    assert model_selection(run, sha, ["portal/src/Page.vue"], check_plan)["included"] == [
        "app",
        "portal",
        "static",
        "unit",
    ]
    assert model_selection(run, sha, ["yarn.lock"], check_plan)["excluded"] == ["unrelated"]
    tests["affected"] = {"required_commands": ["manual"], "dependencies": {"portal": ["unit"]}}
    assert model_selection(run, sha, app_change, check_plan)["excluded"] == ["studio", "unrelated"]
    tests["affected"] = None
    run["test_selection_contract"] = 4
    assert model_selection(run, sha, app_change, check_plan)["excluded"] == ["manual", "unrelated"]
    run["test_selection_contract"] = AUTOMATIC_TEST_CONTRACT
    manifest = manifests["."] | {"workspaces": ["portal"]}
    (repo / "package.json").write_text(json.dumps(manifest))
    git.commit("상위 워크스페이스 선언")
    assert not deferred_builds(run, git.sha(), run_test_policy(run))


def test_required_regression_cannot_be_filtered(candidate):
    svc, rid, repo = candidate
    run = svc.store.get(rid)
    run["config"]["tests"]["affected"] = {"required_commands": ["unit"]}
    with pytest.raises(PipelineError, match="unfiltered"):
        select_tests(run, repo, run["base_sha"], plan=plan())


@pytest.fixture
def gradle_candidate(candidate):
    svc, rid, repo = candidate
    run = svc.store.get(rid)
    (repo / "settings.gradle").write_text("include 'api', 'api:child', 'core'\n")
    tests = run["config"]["tests"]
    tests["commands"] = {}
    for directory in ("api", "api/child", "core"):
        path = repo / directory
        path.mkdir(parents=True)
        (path / "build.gradle").touch()
        prefix = ["gradle", "--offline", "-g", ".cache/gradle", "--rerun-tasks"]
        name = directory.replace("/", "_")
        tests["commands"][name + "_build"] = {
            "argv": prefix + ["assemble"],
            "kind": "check",
            "working_directory": directory,
        }
        tests["commands"][name] = {
            "argv": prefix + ["test"],
            "working_directory": directory,
            "junit_report": directory + "/build/test-results/test/TEST-*.xml",
        }
    git = Git(repo)
    git.commit("Gradle 모듈 준비")
    run.update(base_sha=git.sha(), test_selection_contract=3)
    for directory, name in (("api", "ApiTest"), ("core", "CoreTest")):
        source = repo / directory / "src/test/java/example" / (name + ".java")
        source.parent.mkdir(parents=True)
        source.write_text(f"package example;\nclass {name} {{}}\n")
    git.commit("관련 Gradle 테스트 추가")
    return run, repo, git.sha()


def gradle_plan():
    return [
        {
            "command_id": module,
            "files": [f"src/test/java/example/{name}.java"],
            "reason": "이번 변경의 인수 조건을 검증합니다.",
        }
        for module, name in (("api", "ApiTest"), ("core", "CoreTest"))
    ]


def test_gradle_selects_exact_classes_and_excludes_nested_unrelated_build(gradle_candidate):
    run, repo, sha = gradle_candidate
    pending = select_tests(run, repo, sha)
    assert pending["mandatory_command_ids"] == ["api_build", "core_build"]
    selection = select_tests(run, repo, sha, plan=gradle_plan())
    assert selection["included"] == ["api", "api_build", "core", "core_build"]
    assert selection["excluded"] == ["api_child", "api_child_build"]
    assert selection["actual_scope"] == "affected" and not selection["expanded"]
    commands = selected_config(run, selection).commands
    assert commands["api"].argv == [
        "gradle",
        "--offline",
        "-g",
        ".cache/gradle",
        "--rerun-tasks",
        ":api:test",
        "--tests",
        "example.ApiTest",
    ]
    assert commands["core"].argv[-3:] == [":core:test", "--tests", "example.CoreTest"]
    assert commands["api_build"].argv[-1] == ":api:assemble"
    assert commands["core_build"].argv[-1] == ":core:assemble"
    legacy = copy.deepcopy(run)
    legacy["test_selection_contract"] = 2
    legacy_plan = [item | {"files": []} for item in gradle_plan()]
    assert "api_child_build" in select_tests(legacy, repo, sha, plan=legacy_plan)["included"]
    with pytest.raises(PipelineError, match="exact relative"):
        select_tests(legacy, repo, sha, plan=gradle_plan())


def test_gradle_retains_explicit_required_build_and_resolves_shared_changes(gradle_candidate):
    run, repo, sha = gradle_candidate
    tests = run["config"]["tests"]
    tests["affected"] = {"required_commands": ["api_child_build"]}
    selection = select_tests(run, repo, sha, plan=gradle_plan())
    assert selection["resolved_commands"]["api_child_build"]["argv"][-1] == "assemble"
    tests["affected"] = None
    (repo / "settings.gradle").write_text("include 'api', 'api:child', 'core'\n// changed\n")
    git = Git(repo)
    git.commit("Gradle 공통 설정 변경")
    assert "api_child_build" in select_tests(run, repo, git.sha())["mandatory_command_ids"]


def test_gradle_respects_qualified_tasks_from_build_root(gradle_candidate):
    run, repo, sha = gradle_candidate
    for command in run["config"]["tests"]["commands"].values():
        module = command["working_directory"].replace("/", ":")
        command["argv"][-1] = f":{module}:{command['argv'][-1]}"
        command["working_directory"] = "."
    targets = gradle_plan()
    for target in targets:
        target["files"] = [target["command_id"] + "/" + path for path in target["files"]]
    selection = select_tests(run, repo, sha, plan=targets)
    assert selection["included"] == ["api", "api_build", "core", "core_build"]
    assert selection["resolved_commands"]["api"]["argv"][-3:] == [
        ":api:test",
        "--tests",
        "example.ApiTest",
    ]
    targets[0]["files"] = targets[1]["files"]
    with pytest.raises(PipelineError, match="belong to the selected module"):
        select_tests(run, repo, sha, plan=targets)


@pytest.mark.parametrize("extra", [["check"], ["--tests", "example.OtherTest"]])
def test_gradle_targeting_rejects_extra_tasks_or_existing_filters(gradle_candidate, extra):
    run, repo, sha = gradle_candidate
    run["config"]["tests"]["commands"]["api"]["argv"] += extra
    with pytest.raises(PipelineError, match="one test task"):
        select_tests(run, repo, sha, plan=gradle_plan())


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


@pytest.mark.parametrize("manual_changed", [None, False, True], ids=["legacy", "app", "manual"])
def test_automatic_selection_reaches_final_approval_without_static_mapping(
    candidate, monkeypatch, manual_changed
):
    from pathlib import Path

    from test_validation_policy import detect
    from test_workflow import FakeAgent
    from test_workspace import edit

    from dev_orchestra.application.plan_approval import binding, decide
    from dev_orchestra.domain.models import AgentResult, TestTarget, WorkspaceResult
    from dev_orchestra.infrastructure import test_runner
    from dev_orchestra.orchestration.runner import Runner

    svc, rid, repo = candidate
    selection_version = 3 if manual_changed is None else 4
    included = ["manual", "static", "unit"] if manual_changed else ["static", "unit"]
    change_path = "main.py"
    preparations, doc_builds = [], []
    if manual_changed is not None:
        change_path = "manual/docs/index.md" if manual_changed else "src/main.js"
        (repo / change_path).parent.mkdir(parents=True)
        (repo / "manual").mkdir(exist_ok=True)
        (repo / "manual/package.json").write_text(
            json.dumps(
                {
                    "scripts": {"build": "docusaurus build"},
                    "dependencies": {"@docusaurus/core": "2.4.3"},
                }
            )
        )
        (repo / "manual/yarn.lock").write_text("# yarn lockfile v1\n")
        (repo / change_path).write_text("seed\n")
        git = Git(repo)
        git.commit("매뉴얼 검증 준비")
        config = svc.store.get(rid)["config"]
        build = next(c for c in detect(repo).candidates if c.command.working_directory == "manual")
        assert "manual/.docusaurus" in build.output_paths
        config["tests"]["commands"]["manual"] = build.command.model_dump()
        config["tests"]["output_paths"].extend(build.output_paths)
        svc.store.update(rid, config=config, base_sha=git.sha())
        if not manual_changed:
            policy_run = svc.store.get(rid)
            policy_run.update(test_selection_contract=4, criteria=[{"id": "AC1"}])
            policy_run["config"]["tests"]["affected"] = {
                "criterion_commands": {"AC1": ["unit", "manual"]}
            }
            selection = model_selection(policy_run, git.sha(), [change_path], plan())
            assert "manual" in selection["included"]
    svc.store.update(rid, test_selection_contract=selection_version)

    execute = test_runner.execute

    def run_command(argv, cwd, *args, **kwargs):
        if cwd.name == "manual":
            assert argv[-3:] == ["yarn", "run", "build"]
            doc_builds.append(cwd)
            return 0, ""
        return execute(argv, cwd, *args, **kwargs)

    monkeypatch.setattr(test_runner, "execute", run_command)

    class RecordingRunner(TestRunner):
        def prepare(self, worktree, config, directory, stop=lambda: None):
            preparations.append(set(config.commands))
            self.preflight = Path(directory)

    class Agent(FakeAgent):
        def run(self, request, *args):
            if request.role == "tester":
                assert request.context["automatic_test_selection"]
                return AgentResult(
                    summary="관련 회귀 테스트 자동 선택",
                    command_ids=included,
                    test_plan=[TestTarget(**item) for item in plan()],
                    coverage=[Coverage(criterion_id="AC1", command_ids=["unit"])],
                )
            return super().run(request, *args)

        def run_workspace(self, request, directory, session, stop):
            if request.role == "developer":
                edit(session, change_path, "implemented\n")
                return WorkspaceResult(summary="요구 기능 수정")
            value = session.run_tests(included, test_plan=plan())
            return WorkspaceTestResult(
                summary="관련 회귀 테스트 자동 실행",
                command_ids=value["selection"]["included"],
                test_evidence_id=value["evidence_id"],
                coverage=[Coverage(criterion_id="AC1", command_ids=["unit"])],
            )

    runner = Runner(svc.store, Agent(), RecordingRunner())
    runner.execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_plan_approval", svc.store.get(rid).get("error")
    assert all("manual" not in commands for commands in preparations)
    decide(svc, rid, binding(svc.store, svc.store.get(rid))["plan_hash"], True)
    runner.execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert run["test_result"]["test_selection"]["selector_version"] == selection_version
    assert run["test_result"]["test_selection"]["included"] == included
    assert [commands for commands in preparations if "manual" in commands] == (
        [{"manual"}] if manual_changed else []
    )
    assert len(doc_builds) == int(bool(manual_changed))
    unit = next(c for c in run["test_result"]["execution"]["commands"] if c["command_id"] == "unit")
    assert unit["total"] == 1
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
