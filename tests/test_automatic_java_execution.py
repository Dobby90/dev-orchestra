"""Optional real Maven/Gradle project launchers, using local tools and isolated caches."""

import os
import shlex
import shutil
from pathlib import Path

import pytest
from test_validation_policy import commit_files
from test_workflow import service as service

from dev_orchestra.application.projects import ProjectService
from dev_orchestra.application.validation import complete_run_validation
from dev_orchestra.config import Config
from dev_orchestra.infrastructure import coordination
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
