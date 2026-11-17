"""Automatic project policy, frozen Runs and real bootstrap evidence without models."""

import json
import subprocess
import sys
import xml.etree.ElementTree as ET

import pytest
from test_backend_api import web as web
from test_validation_policy import commit_files
from test_workflow import service as service

from dev_orchestra.application.projects import ProjectService
from dev_orchestra.application.validation import complete_run_validation
from dev_orchestra.config import Config
from dev_orchestra.infrastructure import unittest_bootstrap


def resolve(repo, spec=""):
    info = ProjectService().validate_repository(repo, "main")
    return complete_run_validation(Config(), info, spec)


def test_existing_tests_are_reused_and_manual_policy_is_preserved(service):
    svc, rid, repo = service
    commit_files(repo, {"package.json": json.dumps({"scripts": {"test": "vitest run"}})})
    config = resolve(repo)
    assert any("npm" == cmd.argv[0] for cmd in config.tests.commands.values())
    assert not any(key.startswith("bootstrap_") for key in config.tests.commands)
    original = svc.config.model_dump()
    info = ProjectService().validate_repository(repo, "main")
    assert complete_run_validation(svc.config, info).tests == svc.config.tests
    assert svc.config.model_dump() == original
    assert svc.store.get(rid)["config"]["tests"]["commands"]


def test_empty_project_registration_preview_submission_freezes_bootstrap(web):
    client, svc, _, repo, _ = web
    # The fixture has no configured runner manifest; remove its test files in a new commit.
    from dev_orchestra.infrastructure.git import Git

    git = Git(repo)
    for name in git.call("ls-files").splitlines():
        if name.startswith("tests/"):
            (repo / name).unlink()
    git.commit("신규 프로젝트 준비")
    response = client.post(
        "/api/projects",
        json={
            "name": "자동 프로젝트",
            "repository_path": str(repo),
            "policy": {"tests": {"commands": {}, "write_paths": [], "output_paths": []}},
        },
    )
    assert response.status_code == 201, response.text
    project = response.json()
    proposal = {
        "name": "계산기",
        "project_id": project["id"],
        "spec": {"kind": "text", "text": "Python 계산기 구현"},
    }
    preview = client.post(f"/api/projects/{project['id']}/run-previews", json=proposal)
    assert preview.status_code == 200, preview.text
    receipt = preview.json()
    assert "bootstrap_unittest" in receipt["resolved_validation"]["commands"]
    submitted = client.post(
        "/api/runs",
        json={**proposal, "preview_token": receipt["preview_token"]},
        headers={"Idempotency-Key": "auto-bootstrap"},
    )
    assert submitted.status_code == 202, submitted.text
    run = svc.store.get(submitted.json()["run_id"])
    assert run["config"]["tests"] == receipt["resolved_validation"]
    assert not client.get(f"/api/projects/{project['id']}").json()["policy"]["tests"]["commands"]


def test_new_node_project_gets_dependency_free_runner(service):
    _, _, repo = service
    commit_files(repo, {"package.json": '{"name":"new-app"}'})
    config = resolve(repo)
    command = config.tests.commands["bootstrap_node_0"]
    assert command.argv[0:2] == ["node", "--test"]
    assert command.junit_report == "build/unit.xml"
    assert "tests" in config.tests.write_paths


@pytest.mark.parametrize(
    "body,success",
    [
        ("self.assertEqual(2 + 2, 4)", True),
        ("self.assertEqual(2 + 2, 5)", False),
        ("self.skipTest('unavailable')", False),
    ],
)
def test_bootstrap_emits_real_junit_for_success_failure_and_skip(tmp_path, body, success):
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_feature.py").write_text(
        "import unittest\nclass Feature(unittest.TestCase):\n    def test_sum(self):\n        "
        + body
        + "\n"
    )
    result = subprocess.run(
        [sys.executable, unittest_bootstrap.__file__], cwd=tmp_path, capture_output=True
    )
    report = ET.parse(tmp_path / "build/junit.xml").getroot()
    assert report.attrib["tests"] == "1"
    passed = result.returncode == 0 and int(report.attrib["skipped"]) == 0
    assert passed == success


def test_bootstrap_empty_suite_does_not_pass(tmp_path):
    (tmp_path / "tests").mkdir()
    result = subprocess.run(
        [sys.executable, unittest_bootstrap.__file__], cwd=tmp_path, capture_output=True
    )
    assert result.returncode != 0
    assert ET.parse(tmp_path / "build/junit.xml").getroot().attrib["tests"] == "0"


def test_bootstrap_node_executes_real_behavior_tests(service):
    import shutil

    if not shutil.which("node"):
        pytest.skip("Node required")
    _, _, repo = service
    commit_files(repo, {"package.json": '{"name":"new-app"}'})
    config = resolve(repo)
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "build").mkdir(exist_ok=True)
    test_file = repo / "tests/sum.test.cjs"
    test_file.write_text(
        "const {test}=require('node:test'); const a=require('node:assert/strict'); test('sum',()=>a.equal(2+2,4));"
    )
    command = config.tests.commands["bootstrap_node_0"]
    result = subprocess.run(command.argv, cwd=repo, capture_output=True)
    assert result.returncode == 0, result.stderr
    from dev_orchestra.infrastructure.test_runner import parse_junit

    assert parse_junit(repo / command.junit_report)["total"] == 1
    test_file.write_text(test_file.read_text().replace("2+2,4", "2+2,5"))
    result = subprocess.run(command.argv, cwd=repo, capture_output=True)
    assert result.returncode != 0
    assert parse_junit(repo / command.junit_report)["failed"] == 1


def test_typescript_bootstrap_executes_typescript_source(service):
    import shutil

    if not shutil.which("node"):
        pytest.skip("Node required")
    _, _, repo = service
    commit_files(repo, {"package.json": '{"name":"ts-app","type":"module"}', "tsconfig.json": "{}"})
    config = resolve(repo, "TypeScript 계산기")
    command = config.tests.commands["bootstrap_node_0"]
    assert "--experimental-transform-types" in command.argv
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "build").mkdir(exist_ok=True)
    (repo / "sum.ts").write_text(
        "export function sum(a: number, b: number): number { return a+b; }"
    )
    (repo / "tests/sum.test.ts").write_text(
        "import {test} from 'node:test'; import {strictEqual} from 'node:assert'; import {sum} from '../sum.ts'; test('typed sum',()=>strictEqual(sum(2,3),5));"
    )
    from dev_orchestra.infrastructure.test_runner import parse_junit

    result = subprocess.run(command.argv, cwd=repo, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert parse_junit(repo / command.junit_report)["total"] == 1
    (repo / "sum.ts").write_text(
        "export function sum(a: number, b: number): number { return a-b; }"
    )
    assert subprocess.run(command.argv, cwd=repo, capture_output=True).returncode != 0
    assert parse_junit(repo / command.junit_report)["failed"] == 1


@pytest.mark.parametrize("manifest", ["pom.xml", "build.gradle.kts"])
def test_java_without_tests_reuses_build_tool_and_test_paths(service, manifest):
    _, _, repo = service
    commit_files(repo, {manifest: "<project/>" if manifest.endswith("xml") else "plugins { java }"})
    config = resolve(repo)
    commands = [c for c in config.tests.commands.values() if c.kind == "test"]
    assert len(commands) == 1
    assert "test" in commands[0].argv
    assert "src/test" in config.tests.write_paths
    assert not any(key.startswith("bootstrap_") for key in config.tests.commands)


def test_new_java_bootstrap_compiles_and_executes_real_java(service):
    from dev_orchestra.infrastructure.java import local_jdks
    from dev_orchestra.infrastructure.test_runner import parse_junit

    if not local_jdks():
        pytest.skip("JDK required")
    _, _, repo = service
    config = resolve(repo, "Java 계산기 구현")
    command = config.tests.commands["bootstrap_java"]
    source = repo / "src/main/java/Sum.java"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("public class Sum { public static int add(int a,int b) { return a+b; } }")
    test = repo / "src/test/java/SumTest.java"
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text(
        'public class SumTest { public static void main(String[] args) { assert Sum.add(2,3)==5 : "sum incorrect"; } }'
    )
    import os

    env = os.environ | command.env
    result = subprocess.run(command.argv, cwd=repo, env=env, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert parse_junit(repo / command.junit_report)["total"] == 1
    source.write_text(source.read_text().replace("a+b", "a-b"))
    assert subprocess.run(command.argv, cwd=repo, env=env, capture_output=True).returncode != 0
    assert parse_junit(repo / command.junit_report)["failed"] == 1


def test_declared_vitest_without_script_runs_typescript_and_jsx(service):
    from pathlib import Path

    from dev_orchestra.infrastructure.test_runner import parse_junit

    modules = Path(__file__).resolve().parents[1] / "node_modules"
    if not (modules / ".bin/vitest").is_file():
        pytest.skip("Local Vitest required")
    _, _, repo = service
    commit_files(
        repo,
        {
            "package.json": json.dumps(
                {
                    "name": "tsx-app",
                    "type": "module",
                    "devDependencies": {"vitest": "*", "typescript": "*"},
                    "dependencies": {"react": "*", "react-dom": "*"},
                }
            )
        },
    )
    (repo / "node_modules").symlink_to(modules, target_is_directory=True)
    config = resolve(repo)
    command = config.tests.commands["bootstrap_vitest_0"]
    (repo / "tests").mkdir(exist_ok=True)
    source = repo / "component.tsx"
    source.write_text(
        "import React from 'react'; export function Count({value}: {value: number}) { return <button>{value + 1}</button>; }"
    )
    (repo / "tests/component.test.tsx").write_text(
        "import React from 'react'; import {test,expect} from 'vitest'; import {renderToStaticMarkup} from 'react-dom/server'; import {Count} from '../component'; test('render TSX',()=>expect(renderToStaticMarkup(<Count value={4}/>)).toBe('<button>5</button>'));"
    )
    result = subprocess.run(command.argv, cwd=repo, capture_output=True)
    assert result.returncode == 0, result.stderr + result.stdout
    assert parse_junit(repo / command.junit_report)["total"] == 1
    source.write_text(source.read_text().replace("value + 1", "value - 1"))
    assert subprocess.run(command.argv, cwd=repo, capture_output=True).returncode != 0
    assert parse_junit(repo / command.junit_report)["failed"] == 1
