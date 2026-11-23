"""Optional real Maven/Gradle project launchers, using local tools and isolated caches."""

import os
import shlex
import shutil
import subprocess
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zipfile import ZipFile

import pytest
from test_validation_policy import commit_files
from test_workflow import service as service

from dev_orchestra.application.automatic_tests import AUTOMATIC_TEST_CONTRACT
from dev_orchestra.application.projects import ProjectService
from dev_orchestra.application.test_selection import select_tests, selected_config
from dev_orchestra.application.validation import complete_run_validation
from dev_orchestra.config import Config
from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as Commands
from dev_orchestra.infrastructure import coordination, java
from dev_orchestra.infrastructure.test_runner import TestRunner

pytestmark = pytest.mark.skipif(
    os.environ.get("DEVORCHESTRA_TEST_SANDBOX") != "1",
    reason="Explicit local tool and OS sandbox integration check",
)


def local_tool(tool):
    installed = shutil.which("mvn" if tool == "maven" else "gradle")
    if installed:
        return Path(installed)
    choices = (
        list(Path("/Applications").glob("*.app/Contents/plugins/maven/lib/maven3/bin/mvn"))
        if tool == "maven"
        else list((Path.home() / ".gradle/wrapper/dists").glob("gradle-8.*/**/bin/gradle"))
    )
    if not choices:
        pytest.skip(f"Local {tool} distribution required")
    return sorted(choices)[-1]


@pytest.mark.parametrize("tool", ["maven", "gradle"])
def test_automatic_java_policy_runs_real_project_launcher(service, tmp_path, monkeypatch, tool):
    executable = local_tool(tool)
    _, _, repo = service
    resource = tmp_path / "resources"
    resource.mkdir(mode=0o700)
    monkeypatch.setattr(coordination, "host_root", lambda: resource)
    launcher = "mvnw" if tool == "maven" else "gradlew"
    files = {
        launcher: "#!/bin/sh\nexec " + shlex.quote(str(executable)) + ' "$@"\n',
        "src/main/java/example/Sum.java": "package example; public class Sum { public static int add(int a,int b) { return a+b; } }",
        "src/test/java/example/SumTest.java": "package example; public class SumTest extends junit.framework.TestCase { public void testAddition() { assertEquals(5,Sum.add(2,3)); } }",
    }
    if tool == "maven":
        files[".mvn/wrapper/maven-wrapper.properties"] = (
            "distributionUrl=https://example.invalid/local-maven.zip\n"
        )
        files["pom.xml"] = (
            """<project xmlns="http://maven.apache.org/POM/4.0.0"><modelVersion>4.0.0</modelVersion><groupId>example</groupId><artifactId>auto-tests</artifactId><version>1</version><properties><maven.compiler.release>11</maven.compiler.release></properties><dependencies><dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>3.8.1</version><scope>test</scope></dependency></dependencies><build><plugins><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-resources-plugin</artifactId><version>3.3.1</version></plugin><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-compiler-plugin</artifactId><version>3.13.0</version></plugin><plugin><groupId>org.apache.maven.plugins</groupId><artifactId>maven-surefire-plugin</artifactId><version>3.2.5</version></plugin></plugins></build></project>"""
        )
    else:
        files["gradle/wrapper/gradle-wrapper.properties"] = (
            "distributionUrl=https://example.invalid/local-gradle.zip\n"
        )
        files["settings.gradle"] = "rootProject.name = 'auto-tests'\n"
        files["build.gradle"] = (
            "plugins { id 'java' }; dependencies { testImplementation files('libs/junit.jar', 'libs/hamcrest.jar') }; test { useJUnit() }\n"
        )
        for coordinate, name in [
            ("junit/junit/4.12/junit-4.12.jar", "junit.jar"),
            ("org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar", "hamcrest.jar"),
        ]:
            jar = Path.home() / ".m2/repository" / coordinate
            if not jar.is_file():
                pytest.skip("Cached JUnit and Hamcrest required")
            target = repo / "libs" / name
            target.parent.mkdir(exist_ok=True)
            shutil.copyfile(jar, target)
    commit_files(repo, files)
    info = ProjectService().validate_repository(repo, "main")
    config = complete_run_validation(Config(), info)
    tests = config.tests.model_copy(deep=True)
    tests.commands = {key: value for key, value in tests.commands.items() if value.kind == "test"}
    assert len(tests.commands) == 1
    assert next(iter(tests.commands.values())).argv[0] == "./" + launcher
    runner = TestRunner()
    runner.prepare(repo, tests, tmp_path / "preflight")
    result = runner.run(repo, tests, tmp_path / "passing")
    assert result["passed"], result
    assert result["commands"][0]["total"] == 1
    source = repo / "src/main/java/example/Sum.java"
    source.write_text(source.read_text().replace("a+b", "a-b"))
    result = runner.run(repo, tests, tmp_path / "failing")
    assert not result["passed"], result
    assert result["commands"][0]["failed"] == 1, result


def test_gradle_uses_remapped_directory_without_running_descendant_tests(
    service, tmp_path, monkeypatch
):
    _, _, repo = service
    gradle = local_tool("gradle")
    monkeypatch.setenv("GRADLE_USER_HOME", str(tmp_path / "empty-host-cache"))
    monkeypatch.setattr(coordination, "host_root", lambda: tmp_path / "resources")
    module = "physical"
    for coordinate, name in [
        ("junit/junit/4.12/junit-4.12.jar", "junit.jar"),
        ("org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar", "hamcrest.jar"),
    ]:
        jar = Path.home() / ".m2/repository" / coordinate
        if not jar.is_file():
            pytest.skip("Cached JUnit and Hamcrest required")
        target = repo / module / "libs" / name
        target.parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(jar, target)
    commit_files(
        repo,
        {
            "gradlew": "#!/bin/sh\nexec " + shlex.quote(str(gradle)) + ' "$@"\n',
            "gradle/wrapper/gradle-wrapper.properties": (
                f"distributionUrl=https://example.invalid/{gradle.parent.parent.name}-bin.zip\n"
            ),
            "settings.gradle": (
                "include ':renamed', ':renamed:child'\n"
                "project(':renamed').projectDir = file('physical')\n"
                "project(':renamed:child').projectDir = file('physical/child')\n"
            ),
            "physical/build.gradle": (
                "plugins { id 'java' }; "
                "dependencies { testImplementation files('libs/junit.jar', 'libs/hamcrest.jar') }; "
                "test { useJUnit() }\n"
            ),
            "physical/child/build.gradle": (
                "tasks.register('test') { doLast { throw new GradleException('unrelated') } }\n"
            ),
        },
    )
    info = ProjectService().validate_repository(repo, "main")
    config = complete_run_validation(Config(), info)
    commit_files(
        repo,
        {
            "physical/src/test/java/example/SelectedTest.java": (
                "package example; public class SelectedTest extends junit.framework.TestCase { "
                "public void testSelected() { assertTrue(true); } }"
            ),
            "physical/src/test/java/example/UnrelatedTest.java": (
                "package example; public class UnrelatedTest extends junit.framework.TestCase { "
                "public void testUnrelated() { fail(); } }"
            ),
        },
    )
    candidate = ProjectService().validate_repository(repo, "main").base_sha
    run = {
        "repo": str(repo),
        "base_sha": info.base_sha,
        "test_scope": "affected",
        "test_selection_contract": AUTOMATIC_TEST_CONTRACT,
        "config": config.model_dump(),
    }
    key = next(
        key
        for key, command in config.tests.commands.items()
        if command.kind == "test" and command.working_directory == module
    )
    plan = [
        {
            "command_id": key,
            "files": ["src/test/java/example/SelectedTest.java"],
            "reason": "changed",
        }
    ]
    selection = select_tests(run, repo, candidate, plan=plan)
    tests = selected_config(run, selection)
    assert {c.working_directory for c in tests.commands.values()} == {module}
    assert "--rerun" in tests.commands[key].argv
    runner = TestRunner()
    runner.prepare(repo, tests, tmp_path / "preflight")
    result = runner.run(repo, tests, tmp_path / "results")
    assert result["passed"], result
    test = next(c for c in result["commands"] if c["command_id"] == key)
    assert test["total"] == 1
    assert test["runtime_adapters"][-1]["adapter"] == "gradle-project-directory"


def test_gradle_resolves_offline_from_run_owned_read_only_cache(service, tmp_path, monkeypatch):
    _, _, repo = service
    executable = local_tool("gradle")
    if jdk := java.select_jdk(17):
        monkeypatch.setenv("JAVA_HOME", jdk)
    host = tmp_path / "host"
    monkeypatch.setenv("GRADLE_USER_HOME", str(host))
    monkeypatch.setenv("PATH", str(executable.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(coordination, "host_root", lambda: tmp_path / "slots")
    repository = tmp_path / "repository"
    jar = repository / "org/example/fixture/1/fixture-1.jar"
    jar.parent.mkdir(parents=True)
    with ZipFile(jar, "w") as archive:
        archive.writestr("fixture.txt", "offline")

    # Prime only the temporary host cache. Stop the loopback repository before using the Run.
    with ThreadingHTTPServer(
        ("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=repository)
    ) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            build = """
repositories { maven { url = uri('http://127.0.0.1:PORT'); allowInsecureProtocol = true; metadataSources { artifact() } } }
configurations { sample }
dependencies { sample 'org.example:fixture:1@jar' }
tasks.register('prime') { doLast { assert configurations.sample.singleFile.isFile() } }
def checkShared = {
    def shared = file(System.getenv('GRADLE_RO_DEP_CACHE')).toPath()
    assert configurations.sample.singleFile.toPath().startsWith(shared)
    try {
        java.nio.file.Files.createFile(shared.resolve('write-probe'))
        throw new GradleException('shared dependencies were writable')
    } catch (java.io.IOException expected) {}
}
tasks.register('assemble') { doLast { checkShared() } }
tasks.register('test') { doLast {
    checkShared()
    file('build/unit.xml').text = '<testsuite><testcase name="offline artifact"/></testsuite>'
} }
""".replace("PORT", str(server.server_port))
            commit_files(
                repo, {"settings.gradle": "rootProject.name='fixture'", "build.gradle": build}
            )
            prime = tmp_path / "prime"
            prime.mkdir()
            (prime / "settings.gradle").write_text("rootProject.name='fixture'")
            (prime / "build.gradle").write_text(build)
            subprocess.run(
                [str(executable), "--no-daemon", "prime"], cwd=prime, check=True, timeout=90
            )
        finally:
            server.shutdown()
            thread.join()

    argv = [
        "gradle",
        "--offline",
        "--no-daemon",
        "-g",
        "cache/user",
        "--project-cache-dir",
        "cache/project",
    ]
    cfg = Commands(
        commands={
            "unit": Command(
                network="loopback", argv=[*argv, "test"], junit_report="build/unit.xml"
            ),
            "build": Command(kind="check", network="loopback", argv=[*argv, "assemble"]),
        },
        output_paths=["cache", "build"],
    )
    preflight = tmp_path / "preflight"
    TestRunner().prepare(repo, cfg, preflight)
    result = TestRunner(preflight=preflight).run(repo, cfg, tmp_path / "result")
    assert result["passed"], result
    assert [r["command_id"] for r in result["commands"]] == ["unit", "build"]
    assert result["commands"][0]["total"] == 1
    assert not list((repo / "cache/user").rglob("fixture-1.jar"))
