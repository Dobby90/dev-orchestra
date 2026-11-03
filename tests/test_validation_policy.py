"""Project tooling discovery and language-independent evidence, without model calls."""

import json
import os
import platform
import shutil
import sys
from pathlib import Path

import pytest
from test_backend_api import project
from test_backend_api import web as web
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.agents.prompts import prompt
from dev_orchestra.application.projects import ProjectService
from dev_orchestra.config import Config
from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as Commands
from dev_orchestra.domain.models import AgentRequest, AgentResult, Coverage, PipelineError
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.test_runner import TestRunner, sandbox_command
from dev_orchestra.orchestration.runner import Runner


def commit_files(repo, files):
    for name, contents in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)
    Git(repo).commit("검증 설정 추가")


def detect(repo):
    return ProjectService([repo.parent]).detect_validation(repo, "main")


def test_detect_mixed_tooling_uses_committed_base_and_never_executes(service):
    _, _, repo = service
    package = {
        "scripts": {"test": "vitest run", "test:browser": "playwright test", "build": "vite build"},
        "devDependencies": {"vitest": "*"},
    }
    commit_files(
        repo,
        {
            "web/package.json": json.dumps(package),
            "service/pom.xml": "<project/>",
            "worker/build.gradle.kts": "plugins { java }",
            "pyproject.toml": "[tool.pytest.ini_options]\n",
        },
    )
    (repo / "web/package.json").write_text('{"scripts":{"test":"touch SHOULD_NOT_EXIST"}}')
    dirty = Git(repo).dirty()
    found = detect(repo)
    assert found.base_sha == Git(repo).sha()
    assert len(found.candidates) == 8
    assert {c.command.working_directory for c in found.candidates} == {
        ".",
        "web",
        "service",
        "worker",
    }
    browser = next(c for c in found.candidates if "Playwright" in c.label)
    assert browser.command.network == "loopback"
    assert browser.command.junit_report == "web/build/test_browser.xml"
    assert browser.command.env["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] == "build/test_browser.xml"
    unit = next(c for c in found.candidates if c.label == "web · test")
    assert "--reporter=junit" in unit.command.argv
    policy = Commands(commands={c.id: c.command for c in found.candidates})
    assert len(policy.commands) == 8
    assert Git(repo).dirty() == dirty and not (repo / "SHOULD_NOT_EXIST").exists()


def test_detect_does_not_invent_scripts_and_handles_invalid_manifests(service):
    _, _, repo = service
    commit_files(
        repo,
        {
            "package.json": '{"scripts":{"build":"vite build"}}',
            "apps/broken/package.json": "invalid",
        },
    )
    found = detect(repo)
    assert len(found.candidates) == 1 and found.candidates[0].command.kind == "check"
    assert found.warnings
    with pytest.raises(ValueError, match="JUnit test"):
        Commands(commands={c.id: c.command for c in found.candidates})


def test_detect_wrappers_package_managers_and_distinct_reports(service):
    _, _, repo = service
    commit_files(
        repo,
        {
            "pom.xml": "<project/>",
            "mvnw": "exit 99",
            "build.gradle": "plugins { id 'java' }",
            "gradlew": "exit 99",
            "package.json": json.dumps(
                {
                    "packageManager": "pnpm@10",
                    "scripts": {"test": "vitest run", "test:unit": "vitest run"},
                    "devDependencies": {"vitest": "*"},
                }
            ),
        },
    )
    found = detect(repo)
    commands = [c.command for c in found.candidates]
    assert any(c.argv[0] == "./mvnw" for c in commands)
    assert any(c.argv[0] == "./gradlew" and "--rerun-tasks" in c.argv for c in commands)
    node = [c for c in commands if c.argv[0] == "pnpm"]
    assert len(node) == 2 and node[0].junit_report != node[1].junit_report
    assert all("--" not in c.argv for c in node)


def test_discovery_api_requires_safe_repository_and_csrf(web, tmp_path):
    client, _, _, repo, _ = web
    body = {"repository_path": str(repo), "base_branch": "main"}
    assert client.post("/api/project-validation/detect", json=body).status_code == 200
    assert (
        client.post(
            "/api/project-validation/detect", json=body, headers={"X-CSRF-Token": "invalid"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/project-validation/detect", json={**body, "repository_path": "/"}
        ).status_code
        != 200
    )
    link = tmp_path / "linked"
    link.symlink_to(repo, target_is_directory=True)
    assert (
        client.post(
            "/api/project-validation/detect", json={**body, "repository_path": str(link)}
        ).status_code
        != 200
    )
    profiles = client.get("/api/validation-profiles").json()
    assert {p["id"] for p in profiles} >= {"pytest", "node-test", "playwright", "maven", "gradle"}
    client.cookies.clear()
    assert client.post("/api/project-validation/detect", json=body).status_code == 401


@pytest.mark.parametrize(
    "tool, report",
    [
        ("maven", "target/surefire-reports/TEST-*.xml"),
        ("gradle", "build/test-results/test/TEST-*.xml"),
    ],
)
def test_multiple_java_reports_are_fresh_bundled_and_failure_blocks(
    service, tmp_path, tool, report
):
    _, _, repo = service
    output = report.split("/", 1)[0]
    directory = str(Path(report).parent)
    code = f"from pathlib import Path;p=Path({directory!r});p.mkdir(parents=True,exist_ok=True);(p/'TEST-a.xml').write_text('<testsuite><testcase name=\"ok\"/></testsuite>');(p/'TEST-b.xml').write_text('<testsuite><testcase name=\"bad\"><failure message=\"mismatch\"/></testcase></testsuite>')"
    cfg = Commands(
        commands={tool: Command(argv=[sys.executable, "-c", code], junit_report=report)},
        output_paths=[output],
    )
    runner = TestRunner()
    first = runner.run(repo, cfg, tmp_path / "first")
    result = first["commands"][0]
    assert first["status"] == "failed" and result["total"] == 2 and result["failed"] == 1
    assert Path(result["junit_path"]).is_file()
    cfg.commands[tool].argv = [sys.executable, "-c", "pass"]
    second = runner.run(repo, cfg, tmp_path / "second")
    assert second["status"] == "parse_error" and not second["passed"]
    assert not list((repo / directory).glob("TEST-*.xml"))


def test_subproject_cwd_and_failing_build_are_not_counted_as_tests(service, tmp_path):
    _, _, repo = service
    commit_files(repo, {"web/source.txt": "source"})
    cfg = Commands(
        commands={
            "unit": Command(
                argv=[
                    sys.executable,
                    "-c",
                    "from pathlib import Path;assert Path('source.txt').read_text()=='source';Path('build/unit.xml').write_text('<testsuite><testcase name=\"ok\"/></testsuite>')",
                ],
                working_directory="web",
                junit_report="web/build/unit.xml",
            ),
            "build": Command(
                kind="check",
                argv=[sys.executable, "-c", "raise SystemExit(2)"],
                working_directory="web",
            ),
        },
        output_paths=["web/build"],
    )
    result = TestRunner().run(repo, cfg, tmp_path / "result")
    assert result["status"] == "failed" and not result["passed"]
    assert result["commands"][0]["total"] == 1
    assert result["commands"][1]["total"] is None
    cfg.commands["build"].argv = [sys.executable, "-c", "pass"]
    assert TestRunner().run(repo, cfg, tmp_path / "second")["passed"]


@pytest.mark.parametrize(
    "field, value",
    [
        ("working_directory", "../outside"),
        ("junit_report", "../outside/*.xml"),
        ("env", {"HOME": "/"}),
    ],
)
def test_command_contract_rejects_unsafe_overrides(field, value):
    with pytest.raises((ValueError, PipelineError)):
        Command.model_validate(
            {"argv": ["npm", "test"], "junit_report": "build/unit.xml", field: value}
        )


def test_checks_cannot_replace_test_coverage(service):
    svc, rid, _ = service
    cfg = Config.model_validate(svc.store.get(rid)["config"])
    cfg.tests.commands["build"] = Command(kind="check", argv=["npm", "run", "build"])
    svc.store.update(rid, config=cfg.model_dump(mode="json"))

    class BuildOnlyCoverage(FakeAgent):
        def run(self, req, *args):
            if req.role == "tester":
                return AgentResult(
                    summary="tests",
                    command_ids=["unit", "build"],
                    coverage=[Coverage(criterion_id="AC1", command_ids=["build"])],
                )
            return super().run(req, *args)

    Runner(svc.store, BuildOnlyCoverage(), FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "failed" and "coverage" in run["error"]


def test_saved_policy_is_not_replaced_by_project_edit_or_resume_answers(web):
    client, svc, rid, repo, _ = web
    p = project(client, svc, repo)
    old = svc.store.get(rid)["config"]
    tests = {"commands": {"node": {"argv": ["npm", "test"], "junit_report": "build/unit.xml"}}}
    response = client.patch(
        f"/api/projects/{p['id']}",
        headers={"If-Match": f'"project:{p["id"]}:v0"'},
        json={"policy": {"tests": tests}},
    )
    assert response.status_code == 200
    svc.store.update(rid, "needs_input")
    changes = svc.resume_changes(svc.store.get(rid), "npm test로 변경")
    assert "config" not in changes and svc.store.get(rid)["config"] == old
    text = prompt(
        AgentRequest(
            run_id=rid,
            role="planner",
            worktree=str(repo),
            iteration=0,
            input_sha="a" * 40,
            context={},
        )
    )
    assert "create a new run" in text and "command IDs and files" in text


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS loopback sandbox")
def test_loopback_sandbox_allows_local_server_and_denies_external_network(service, tmp_path):
    _, _, repo = service
    code = """import socket
from pathlib import Path
s=socket.socket();s.bind(('127.0.0.1',0));s.listen()
c=socket.create_connection(s.getsockname(),timeout=2)
a,_=s.accept();c.send(b'ok');assert a.recv(2)==b'ok'
c.close();a.close();s.close()
try:
 socket.create_connection(('203.0.113.1',80),timeout=1)
except PermissionError:
 pass
else:
 raise AssertionError('external network allowed')
Path('build/unit.xml').write_text('<testsuite><testcase name="loopback"/></testsuite>')
"""
    cfg = Commands(
        commands={
            "browser": Command(
                argv=[sys.executable, "-c", code], junit_report="build/unit.xml", network="loopback"
            )
        },
        output_paths=["build"],
    )
    result = TestRunner().run(repo, cfg, tmp_path / "loopback")
    assert result["passed"], result


def test_loopback_linux_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/bwrap")
    with pytest.raises(PipelineError, match="loopback"):
        sandbox_command(tmp_path, [], tmp_path, ["true"], "loopback")


def test_recursive_java_glob_rejects_reports_outside_outputs(service, tmp_path):
    _, _, repo = service
    commit_files(repo, {"pom.xml": "<project/>", "child/pom.xml": "<project/>"})
    found = detect(repo)
    root = next(c for c in found.candidates if c.label == ". · Maven 테스트")
    assert root.command.junit_report == "**/target/surefire-reports/TEST-*.xml"
    assert "child/target" in root.output_paths
    code = "from pathlib import Path;p=Path('child/target/surefire-reports');p.mkdir(parents=True,exist_ok=True);(p/'TEST-child.xml').write_text('<testsuite><testcase name=\"ok\"/></testsuite>')"
    cfg = Commands(
        commands={
            "java": Command(
                argv=[sys.executable, "-c", code], junit_report=root.command.junit_report
            )
        },
        output_paths=root.output_paths,
    )
    assert TestRunner().run(repo, cfg, tmp_path / "first")["passed"]
    unapproved = repo / "unapproved/target/surefire-reports/TEST-other.xml"
    unapproved.parent.mkdir(parents=True)
    unapproved.write_text('<testsuite><testcase name="not-authorized"/></testsuite>')
    with pytest.raises(PipelineError, match="configured output"):
        TestRunner().run(repo, cfg, tmp_path / "second")
    assert unapproved.exists()


def test_results_counts_exclude_build_checks(web, tmp_path):
    from dev_orchestra.application.results import Results
    from dev_orchestra.infrastructure.files import write

    _, svc, rid, _, _ = web
    run = svc.store.get(rid)
    sha = "a" * 40
    sid, _ = svc.store.start_step(rid, "tester", 0, sha)
    artifact = Path(run["directory"]) / "counts-result.json"
    payload = {
        "sha": sha,
        "execution": {
            "passed": False,
            "status": "failed",
            "commands": [
                {
                    "command_id": "unit",
                    "kind": "test",
                    "duration_seconds": 1,
                    "total": 2,
                    "failed": 0,
                    "skipped": 0,
                },
                {
                    "command_id": "build",
                    "kind": "check",
                    "duration_seconds": 1,
                    "total": None,
                    "failed": None,
                    "skipped": None,
                },
            ],
        },
    }
    checksum = write(artifact, payload)
    svc.store.finish_step(sid, sha, {"artifact": str(artifact), "hash": checksum})
    result = Results(svc.store).tests(rid)
    assert result["status"] == "failed" and result["total"] == result["passed"] == 2


@pytest.mark.skipif(not shutil.which("npm") or not shutil.which("node"), reason="Node/npm required")
def test_real_npm_test_and_build_commands_in_sandbox(service, tmp_path):
    _, _, repo = service
    commit_files(
        repo,
        {
            "package.json": json.dumps(
                {
                    "scripts": {
                        "test": "node --test --test-reporter=junit --test-reporter-destination=build/test.xml tests/unit.test.cjs",
                        "build": "node build.cjs",
                    }
                }
            ),
            "tests/unit.test.cjs": "const test=require('node:test');const assert=require('node:assert/strict');test('addition',()=>assert.equal(2+3,5));",
            "build.cjs": "require('node:fs').writeFileSync('build/output.txt','built');",
        },
    )
    detected = detect(repo)
    cfg = Commands(commands={c.id: c.command for c in detected.candidates}, output_paths=["build"])
    result = TestRunner().run(repo, cfg, tmp_path / "npm")
    assert result["passed"], result
    assert next(c for c in result["commands"] if c["kind"] == "test")["total"] == 1
    assert (repo / "build/output.txt").read_text() == "built"


def test_preview_warns_about_saved_pytest_policy_on_node_repository(web):
    client, svc, _, repo, _ = web
    commit_files(
        repo, {"package.json": '{"scripts":{"test":"vitest run"},"devDependencies":{"vitest":"*"}}'}
    )
    p = project(client, svc, repo)
    response = client.post(
        f"/api/projects/{p['id']}/run-previews",
        json={"project_id": p["id"], "spec": {"kind": "text", "text": "Node 기능 검증"}},
    )
    assert response.status_code == 200, response.text
    warnings = response.json()["validation_warnings"]
    assert len(warnings) == 1 and "pytest" in warnings[0] and "프로젝트 검증 설정" in warnings[0]
    assert client.get(f"/api/projects/{p['id']}").json()["policy"]["tests"]["commands"]["unit"][
        "argv"
    ] == ["pytest"]


def test_detection_protects_existing_colocated_node_tests(service):
    _, _, repo = service
    commit_files(
        repo,
        {
            "package.json": '{"scripts":{"test":"vitest run"}}',
            "src/widget.test.tsx": "existing tests",
            "src/helper.spec.ts": "existing tests",
        },
    )
    item = next(c for c in detect(repo).candidates if c.command.kind == "test")
    assert {"src/widget.test.tsx", "src/helper.spec.ts"} <= set(item.write_paths)


@pytest.mark.skipif(
    os.environ.get("DEVORCHESTRA_TEST_BROWSER_SANDBOX") != "1",
    reason="opt-in actual Chromium sandbox test",
)
def test_actual_playwright_browser_inside_sandbox(service, tmp_path):
    from dev_orchestra.application.validation import candidate

    _, _, repo = service
    commit_files(
        repo,
        {
            "package.json": '{"scripts":{"test:browser":"playwright test --workers=1"}}',
            "smoke.spec.cjs": """const {test,expect}=require('@playwright/test');
const http=require('node:http');
test('local browser verification',async({page})=>{
 const server=http.createServer((req,res)=>res.end('<h1>verified</h1>'));
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
 try {await page.goto('http://127.0.0.1:'+server.address().port);await expect(page.locator('h1')).toHaveText('verified');}
 finally {server.close();}
});""",
        },
    )
    (repo / "node_modules").symlink_to(
        Path(__file__).resolve().parents[1] / "node_modules", target_is_directory=True
    )
    item = candidate("playwright")
    if "PLAYWRIGHT_BROWSERS_PATH" in os.environ:
        item.command.env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    result = TestRunner().run(
        repo,
        Commands(commands={"browser": item.command}, output_paths=item.output_paths),
        tmp_path / "browser",
    )
    assert result["passed"], result
    assert result["commands"][0]["total"] == 1


def test_unsupported_loopback_is_rejected_before_model_calls(service, monkeypatch):
    svc, rid, _ = service
    config = Config.model_validate(svc.store.get(rid)["config"])
    config.tests.commands["unit"].network = "loopback"
    svc.store.update(rid, config=config.model_dump(mode="json"))
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    agent = FakeAgent()
    Runner(svc.store, agent, TestRunner()).execute(rid)
    assert agent.calls == []
    run = svc.store.get(rid)
    assert run["state"] == "failed" and "requires macOS" in run["error"]
