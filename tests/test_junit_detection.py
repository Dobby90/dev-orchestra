"""Project-specific report discovery; only disposable repositories and real local tests."""

import json
import shutil
from pathlib import Path

import pytest
from test_backend_api import project
from test_backend_api import web as web
from test_validation_policy import commit_files, detect
from test_workflow import service as service

from dev_orchestra.application.projects import ProjectService
from dev_orchestra.application.validation import node_junit_report, policy_warnings
from dev_orchestra.config import Config
from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as Commands
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.test_runner import TestRunner


def native_project(repo, script="node --test tests/*.test.mjs", **scripts):
    commit_files(
        repo,
        {
            "package.json": json.dumps({"scripts": {"test": script, **scripts}}),
            "tests/first.test.mjs": "import {test} from 'node:test';test('first',()=>{});\n",
        },
    )


def policy(command, outputs=("build",)):
    return Config(tests=Commands(commands={"unit": command}, output_paths=list(outputs)))


def info(repo):
    return ProjectService([repo.parent]).validate_repository(repo, "main")


def test_plain_native_node_script_gets_file_report_without_repository_mutation(service):
    _, _, repo = service
    native_project(repo)
    (repo / "package.json").write_text('{"scripts":{"test":"touch NEVER_EXECUTE"}}')
    before = Git(repo).dirty()
    item = detect(repo).candidates[0]
    assert item.command.argv == [
        "npm",
        "--node-options=--test-reporter=spec --test-reporter-destination=stdout --test-reporter=junit --test-reporter-destination=build/test.xml",
        "run",
        "test",
    ]
    assert item.command.junit_report == "build/test.xml"
    assert not item.warnings
    assert Git(repo).dirty() == before and not (repo / "NEVER_EXECUTE").exists()


@pytest.mark.parametrize("manager", ["npm", "pnpm", "yarn"])
def test_existing_junit_destination_and_subproject_output_are_preserved(service, manager):
    _, _, repo = service
    commit_files(
        repo,
        {
            "web/package.json": json.dumps(
                {
                    "packageManager": manager + "@1",
                    "scripts": {
                        "test": "node --test --test-reporter=spec --test-reporter-destination=stdout --test-reporter junit --test-reporter-destination './reports/unit.xml' tests/*.test.mjs"
                    },
                }
            ),
        },
    )
    item = detect(repo).candidates[0]
    assert item.command.argv == [manager, "run", "test"]
    assert item.command.junit_report == "web/reports/unit.xml"
    assert "web/reports" in item.output_paths
    assert not item.warnings


@pytest.mark.parametrize(
    "script",
    [
        "node --test --test-reporter=junit tests/*.test.mjs",
        "node --test tests/*.test.mjs --test-reporter=junit --test-reporter-destination=build/test.xml",
        "node --test --test-reporter=junit --test-reporter-destination=../outside.xml tests/*.test.mjs",
        "node --test --test-reporter=junit --test-reporter-destination=.env/report.xml tests/*.test.mjs",
        "node --test --watch tests/*.test.mjs",
    ],
)
def test_ambiguous_native_report_settings_are_not_overwritten(service, script):
    _, _, repo = service
    native_project(repo, script)
    item = detect(repo).candidates[0]
    assert item.command.argv == ["npm", "run", "test"]
    assert item.warnings and "파일 경로 앞" in item.warnings[0]
    assert "build/test.xml" in item.warnings[0]


@pytest.mark.parametrize("kind", ["hook", "npmrc", "pnpm", "yarn"])
def test_existing_lifecycle_configuration_and_package_managers_are_kept(service, kind):
    _, _, repo = service
    package = {"scripts": {"test": "node --test tests/*.test.mjs"}}
    if kind == "hook":
        package["scripts"]["pretest"] = "node prepare.cjs"
    elif kind in {"pnpm", "yarn"}:
        package["packageManager"] = kind + "@1"
    files = {"package.json": json.dumps(package)}
    if kind == "npmrc":
        files[".npmrc"] = "node-options=--import=./loader.mjs\n"
    commit_files(repo, files)
    item = detect(repo).candidates[0]
    assert item.command.argv == [kind if kind in {"pnpm", "yarn"} else "npm", "run", "test"]
    assert item.warnings


@pytest.mark.parametrize(
    "script",
    [
        "node prepare.cjs && node --test tests/*.test.mjs",
        "echo playwright && node --test tests/*.test.mjs",
        "echo vitest && node --test tests/*.test.mjs",
        "NODE_OPTIONS=--import=./loader.mjs node --test tests/*.test.mjs",
    ],
)
def test_compound_scripts_do_not_receive_guessed_runner_options(service, script):
    _, _, repo = service
    commit_files(
        repo,
        {
            "package.json": json.dumps(
                {"scripts": {"test": script}, "devDependencies": {"vitest": "*"}}
            )
        },
    )
    item = detect(repo).candidates[0]
    assert item.command.argv == ["npm", "run", "test"]
    assert item.warnings


def test_report_directory_cannot_authorize_writes_to_tracked_source(service):
    _, _, repo = service
    native_project(
        repo,
        "node --test --test-reporter=junit --test-reporter-destination=tests/result.xml tests/*.test.mjs",
    )
    item = detect(repo).candidates[0]
    assert "tests" not in item.output_paths
    assert item.warnings


def test_root_and_subproject_use_local_report_options_and_root_collection_paths(service):
    _, _, repo = service
    script = {"scripts": {"test": "node --test tests/*.test.mjs"}}
    commit_files(repo, {"package.json": json.dumps(script), "web/package.json": json.dumps(script)})
    items = detect(repo).candidates
    assert {i.command.junit_report for i in items} == {"build/test.xml", "web/build/test.xml"}
    assert all("--test-reporter-destination=build/test.xml" in i.command.argv[1] for i in items)


def test_saved_policy_warns_for_missing_report_but_never_changes_commands(service):
    _, _, repo = service
    native_project(repo)
    cfg = policy(Command(argv=["npm", "run", "test"], junit_report="build/test.xml"))
    before = cfg.model_dump()
    warnings = policy_warnings(cfg, info(repo))
    assert any("JUnit 파일" in w and "새 작업" in w and "unit" in w for w in warnings)
    assert cfg.model_dump() == before
    item = detect(repo).candidates[0]
    assert not policy_warnings(policy(item.command), info(repo))


def test_saved_policy_compares_actual_report_path_and_allowed_outputs(service):
    _, _, repo = service
    native_project(
        repo,
        "node --test --test-reporter=junit --test-reporter-destination=build/actual.xml tests/*.test.mjs",
    )
    cfg = policy(
        Command(argv=["npm", "run", "test"], junit_report="build/expected.xml"), outputs=("other",)
    )
    warnings = policy_warnings(cfg, info(repo))
    assert any("build/actual.xml" in w and "build/expected.xml" in w for w in warnings)
    assert any("출력 경로" in w for w in warnings)
    aligned = policy(Command(argv=["npm", "run", "test"], junit_report="./build/actual.xml"))
    assert not policy_warnings(aligned, info(repo))


def test_playwright_report_env_is_compared_instead_of_assuming_default_path(service):
    _, _, repo = service
    commit_files(repo, {"package.json": '{"scripts":{"test:browser":"playwright test"}}'})
    item = detect(repo).candidates[0]
    item.command.env["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] = "build/custom.xml"
    warnings = policy_warnings(policy(item.command), info(repo))
    assert any("build/custom.xml" in w and "build/test_browser.xml" in w for w in warnings)


def test_native_detection_and_old_policy_warning_are_available_through_api(web):
    client, svc, _, repo, _ = web
    native_project(repo)
    detected = client.post(
        "/api/project-validation/detect", json={"repository_path": str(repo), "base_branch": "main"}
    )
    assert detected.status_code == 200, detected.text
    assert "--node-options=" in detected.json()["candidates"][0]["command"]["argv"][1]
    svc.config.tests = Commands(
        commands={"unit": Command(argv=["npm", "run", "test"], junit_report="build/test.xml")},
        output_paths=["build"],
    )
    p = project(client, svc, repo)
    response = client.post(
        f"/api/projects/{p['id']}/run-previews",
        json={
            "name": "테스트 작업",
            "project_id": p["id"],
            "spec": {"kind": "text", "text": "검증"},
        },
    )
    assert response.status_code == 200, response.text
    assert any("JUnit 파일" in w for w in response.json()["validation_warnings"])
    stored = client.get(f"/api/projects/{p['id']}").json()["policy"]["tests"]["commands"]["unit"]
    assert stored["argv"] == ["npm", "run", "test"]


@pytest.mark.skipif(not shutil.which("npm") or not shutil.which("node"), reason="Node/npm required")
@pytest.mark.parametrize("failing", [False, True])
def test_detected_native_command_produces_real_junit_and_preserves_failures(
    service, tmp_path, failing
):
    _, _, repo = service
    native_project(repo)
    item = detect(repo).candidates[0]
    # Added tests are picked up by the original script glob after policy creation.
    commit_files(
        repo,
        {
            "tests/later.test.mjs": "import {test} from 'node:test';test('later',()=>{"
            + ("throw new Error('expected failure');" if failing else "")
            + "});\n"
        },
    )
    cfg = Commands(commands={item.id: item.command}, output_paths=item.output_paths)
    result = TestRunner().run(repo, cfg, tmp_path / "native")
    command = result["commands"][0]
    assert command["total"] == 2, result
    assert command["failed"] == int(failing)
    assert result["passed"] is not failing
    assert (repo / "build/test.xml").is_file()
    assert Path(command["junit_path"]).is_file()


def test_node_reporter_options_after_files_are_not_treated_as_options():
    assert (
        node_junit_report(
            [
                "node",
                "--test",
                "tests/a.test.mjs",
                "--test-reporter=junit",
                "--test-reporter-destination=build/test.xml",
            ]
        )
        is None
    )
    assert (
        node_junit_report(
            [
                "node",
                "--test",
                "--test-reporter=junit",
                "--test-reporter-destination=build/test.xml",
                "tests/a.test.mjs",
            ]
        )
        == "build/test.xml"
    )
