"""Java readiness must precede model work and preserve source, policy and host caches."""

from pathlib import Path

import pytest
from test_validation_policy import commit_files, detect
from test_workflow import FakeAgent
from test_workflow import service as service

from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as Commands
from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure import java
from dev_orchestra.infrastructure.dependencies import prepare_dependencies
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.test_runner import TestRunner
from dev_orchestra.orchestration.runner import Runner


@pytest.fixture
def jdk(tmp_path, monkeypatch):
    root = tmp_path / "jdk-17"
    (root / "bin").mkdir(parents=True)
    for name in ("java", "javac"):
        path = root / "bin" / name
        path.write_text(f"#!/bin/sh\nprintf '{name} 17.0.1\\n'\n")
        path.chmod(0o755)
    (root / "release").write_text('JAVA_VERSION="17.0.1"\n')
    monkeypatch.setattr(java, "local_jdks", lambda: {root: 17})
    return root


def java_config(tool="maven", directory="."):
    prefix = "" if directory == "." else directory + "/"
    command = Command(
        argv=["./mvnw" if tool == "maven" else "./gradlew", "test"],
        working_directory=directory,
        junit_report=prefix + "build/unit.xml",
    )
    if tool == "maven":
        command.argv += ["-o", "-Dmaven.repo.local=.devorchestra-cache/maven"]
        outputs = [prefix + ".devorchestra-cache/maven"]
    else:
        command.argv += [
            "--offline",
            "--no-daemon",
            "--gradle-user-home",
            ".devorchestra-cache/gradle",
            "--project-cache-dir",
            ".devorchestra-cache/project",
        ]
        command.network = "loopback"
        outputs = [prefix + ".devorchestra-cache"]
    return Commands(commands={"java": command}, output_paths=[prefix + "build", *outputs])


def wrapper(repo, tool):
    name = "mvnw" if tool == "maven" else "gradlew"
    props = (
        ".mvn/wrapper/maven-wrapper.properties"
        if tool == "maven"
        else "gradle/wrapper/gradle-wrapper.properties"
    )
    distro = "apache-maven-3.9.9-bin" if tool == "maven" else "gradle-8.10-bin"
    commit_files(
        repo,
        {
            name: "#!/bin/sh\nexit 0\n",
            props: f"distributionUrl=https\\://example.invalid/{distro}.zip\n",
        },
    )
    (repo / name).chmod(0o755)
    Git(repo).commit("Wrapper 실행 권한 설정")
    return distro


def mock_tools(monkeypatch, calls, *, fails=False):
    monkeypatch.setattr(java, "sandbox_command", lambda w, o, s, argv, network="none": argv)

    def execute(argv, cwd, timeout, log, **kwargs):
        calls.append((argv, kwargs["env"]))
        return (
            (0, "javac 17.0.1")
            if argv[-1] == "-version"
            else ((1, "bootstrap failed") if fails else (0, "ready"))
        )

    monkeypatch.setattr(java, "execute", execute)


@pytest.mark.parametrize(
    "name,text,expected",
    [
        (
            "pom.xml",
            '<project xmlns="urn:maven"><properties><java.version>17</java.version></properties></project>',
            17,
        ),
        (
            "pom.xml",
            "<project><properties><maven.compiler.release>21</maven.compiler.release></properties></project>",
            21,
        ),
        (
            "build.gradle.kts",
            "java { toolchain { languageVersion.set(JavaLanguageVersion.of(17)) } }",
            17,
        ),
        ("build.gradle", "sourceCompatibility = JavaVersion.VERSION_1_8", 8),
        ("build.gradle.kts", "kotlin { jvmToolchain(21) }", 21),
    ],
)
def test_literal_jdk_requirements(name, text, expected):
    assert java.required_jdk({name: text}.get) == expected
    assert java.project_jdk({name: text}.get, "service") == expected


@pytest.mark.parametrize(
    "version,jvm,supported",
    [
        ((7, 2, 0), 17, False),
        ((7, 3, 0), 17, True),
        ((8, 4, 0), 21, False),
        ((8, 5, 0), 21, True),
        ((9, 0, 0), 11, False),
        ((9, 0, 0), 17, True),
    ],
)
def test_gradle_runtime_compatibility(version, jvm, supported):
    assert java.gradle_jvm_supported(version, jvm) is supported


def test_gradle_runtime_and_compiler_toolchain_are_selected_separately(tmp_path, monkeypatch):
    j17, j21 = tmp_path / "jdk17", tmp_path / "jdk21"
    monkeypatch.setattr(java, "local_jdks", lambda: {j17: 17, j21: 21})
    files = {
        "build.gradle.kts": "java { toolchain { languageVersion.set(JavaLanguageVersion.of(21)) } }",
        "gradle/wrapper/gradle-wrapper.properties": "distributionUrl=https://example.invalid/gradle-8.4-bin.zip",
    }
    assert java.choose_project_jdk(files.get, tool="gradle") == str(j17)
    monkeypatch.setattr(java, "local_jdks", lambda: {j21: 21})
    assert java.choose_project_jdk(files.get, tool="gradle") is None


def test_build_syntax_errors_and_commented_toolchains_are_not_environment_failures():
    files = {"pom.xml": "<project><broken>"}
    assert java.required_jdk(files.get) is None
    assert java.declared_artifacts(files.get, "maven") == []
    files = {"build.gradle.kts": "// JavaLanguageVersion.of(21)\nsourceCompatibility = '17'"}
    assert java.required_jdk(files.get) == 17
    assert not java.has_toolchain(files.get)


def test_explicit_java_home_is_expanded_consistently_for_preparation_and_tests(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    command = java_config().commands["java"]
    command.env["JAVA_HOME"] = "~/jdks/17"
    assert java.java_environment(tmp_path, command)["JAVA_HOME"] == str(tmp_path / "jdks/17")


@pytest.mark.parametrize("tool", ["maven", "gradle"])
def test_new_java_run_freezes_local_jdk_and_isolated_caches(service, jdk, tool):
    svc, old_id, repo = service
    manifest = "pom.xml" if tool == "maven" else "build.gradle"
    text = (
        "<project><properties><java.version>17</java.version></properties></project>"
        if tool == "maven"
        else "java { toolchain { languageVersion = JavaLanguageVersion.of(17) } }"
    )
    commit_files(repo, {manifest: text})
    command = Command(
        argv=["mvn" if tool == "maven" else "gradle", "test"], junit_report="build/unit.xml"
    )
    svc.config.tests = Commands(commands={"java": command})
    before, old = svc.config.model_dump(), svc.store.get(old_id)["config"]
    new_id = svc.create(repo, Path(svc.store.get(old_id)["directory"]) / "spec.md")
    config = svc.store.get(new_id)["config"]["tests"]
    frozen = config["commands"]["java"]
    assert frozen["env"]["JAVA_HOME"] == str(jdk)
    assert "--offline" in frozen["argv"]
    assert any(p.startswith(".devorchestra-cache/") for p in config["output_paths"])
    assert svc.config.model_dump() == before and svc.store.get(old_id)["config"] == old


def test_non_ascii_module_uses_committed_jdk_requirement(service, jdk):
    svc, old_id, repo = service
    directory = "서비스 api"
    manifest = repo / directory / "pom.xml"
    commit_files(
        repo,
        {
            directory
            + "/pom.xml": "<project><properties><java.version>17</java.version></properties></project>"
        },
    )
    manifest.write_text(
        "<project><properties><java.version>21</java.version></properties></project>"
    )
    svc.config.tests = java_config(directory=directory)
    rid = svc.create(repo, Path(svc.store.get(old_id)["directory"]) / "spec.md")
    config = svc.store.get(rid)["config"]["tests"]
    assert config["commands"]["java"]["env"]["JAVA_HOME"] == str(jdk)
    assert "21" in manifest.read_text()


def test_cache_copy_excludes_credentials_locks_and_symlinks_without_shared_inodes(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    for name in (
        "lib.jar",
        "settings.xml",
        "settings-security.xml",
        "gradle.properties",
        "modules.lock",
        "gc.properties",
        "private.pem",
    ):
        (source / name).write_text("original")
    (source / "escape.jar").symlink_to(source / "settings.xml")
    java.seed_cache(source, target, lambda: None)
    assert {p.name for p in target.iterdir()} == {"lib.jar"}
    (target / "lib.jar").write_text("changed")
    assert (source / "lib.jar").read_text() == "original"


@pytest.mark.parametrize("tool", ["maven", "gradle"])
def test_wrapper_and_dependency_cache_are_reused_offline(service, tmp_path, jdk, monkeypatch, tool):
    _, _, repo = service
    distro = wrapper(repo, tool)
    host = tmp_path / "host"
    src = host / (
        "repository/example/lib/1.0/lib-1.0.jar"
        if tool == "maven"
        else "caches/modules-2/files-2.1/example/lib/1.0/hash/lib-1.0.jar"
    )
    src.parent.mkdir(parents=True)
    src.write_text("artifact")
    dist = host / "wrapper/dists" / distro / "hash/bin/tool"
    dist.parent.mkdir(parents=True)
    dist.write_text("cached distro")
    dist.chmod(0o755)
    monkeypatch.setattr(java, "host_cache", lambda _: host)
    calls = []
    mock_tools(monkeypatch, calls)
    cfg = java_config(tool)
    before = cfg.model_dump()
    java.prepare_java(repo, cfg, tmp_path / "preflight")
    root = repo / ".devorchestra-cache" / ("maven" if tool == "maven" else "gradle")
    assert (root / "wrapper/dists" / distro / "hash/bin/tool").read_text() == "cached distro"
    assert len(calls) == 2 and calls[1][0] == [cfg.commands["java"].argv[0], "--version"]
    env = calls[1][1]
    assert env["JAVA_HOME"] == str(jdk) and env["HOME"] != str(Path.home())
    assert env["MAVEN_USER_HOME" if tool == "maven" else "GRADLE_USER_HOME"] == str(root)
    assert cfg.model_dump() == before and src.read_text() == "artifact"
    assert not Git(repo, cfg.output_paths).dirty()


def test_missing_jdk_stops_before_any_model_call(service, monkeypatch):
    svc, rid, repo = service
    commit_files(
        repo,
        {"pom.xml": "<project><properties><java.version>21</java.version></properties></project>"},
    )
    monkeypatch.setattr(java, "local_jdks", lambda: {})
    svc.config.tests = java_config()
    rid = svc.create(repo, Path(svc.store.get(rid)["directory"]) / "spec.md")
    agent = FakeAgent()
    Runner(svc.store, agent, TestRunner())._execute(rid)
    result = svc.store.get(rid)
    assert result["state"] == "needs_input" and "JDK 21" in result["error"]
    assert not agent.calls


def test_missing_declared_dependency_is_reported_before_tool_or_model_work(
    service, tmp_path, jdk, monkeypatch
):
    _, _, repo = service
    wrapper(repo, "maven")
    commit_files(
        repo,
        {
            "pom.xml": "<project><dependencies><dependency><groupId>example</groupId><artifactId>lib</artifactId><version>1.0</version></dependency></dependencies></project>"
        },
    )
    monkeypatch.setattr(java, "host_cache", lambda _: tmp_path / "empty")
    monkeypatch.setattr(
        java, "execute", lambda *a, **kw: pytest.fail("must report missing dependency first")
    )
    with pytest.raises(PipelineError, match="example:lib:1.0"):
        java.prepare_java(repo, java_config(), tmp_path / "preflight")


@pytest.mark.parametrize("unsafe", ["symlink", "tracked", "tracked_unicode", "outside"])
def test_java_cache_writes_never_escape_or_include_source(
    service, tmp_path, jdk, monkeypatch, unsafe
):
    _, _, repo = service
    wrapper(repo, "maven")
    cfg = java_config()
    cache = repo / ".devorchestra-cache/maven"
    if unsafe == "symlink":
        cache.parent.mkdir()
        cache.symlink_to(tmp_path / "outside")
    elif unsafe.startswith("tracked"):
        name = "보호 소스.java" if unsafe == "tracked_unicode" else "source.java"
        commit_files(repo, {".devorchestra-cache/maven/" + name: "preserve"})
    else:
        cfg.commands["java"].argv[-1] = "-Dmaven.repo.local=../outside"
    monkeypatch.setattr(
        java, "execute", lambda *a, **kw: pytest.fail("unsafe cache must not execute")
    )
    monkeypatch.setattr(java, "host_cache", lambda _: tmp_path / "empty")
    with pytest.raises(PipelineError):
        java.prepare_java(repo, cfg, tmp_path / "preflight")


def test_execution_also_rejects_tracked_non_ascii_source_in_java_cache(service, tmp_path):
    _, _, repo = service
    commit_files(repo, {".devorchestra-cache/maven/보호.java": "preserve"})
    with pytest.raises(PipelineError, match="tracked source"):
        TestRunner().run(repo, java_config(), tmp_path / "results")


def test_manually_prepared_custom_wrapper_is_not_rejected(service, tmp_path, jdk, monkeypatch):
    _, _, repo = service
    wrapper(repo, "maven")
    props = repo / ".mvn/wrapper/maven-wrapper.properties"
    props.write_text("distributionUrl=https://example.invalid/custom-tool.zip\n")
    Git(repo).commit("사용자 지정 Wrapper 배포본 설정")
    monkeypatch.setattr(java, "host_cache", lambda _: tmp_path / "empty")
    calls = []
    mock_tools(monkeypatch, calls)
    java.prepare_java(repo, java_config(), tmp_path / "preflight")
    assert len(calls) == 2


def test_readiness_failure_is_resumable_and_cancel_is_preserved(
    service, tmp_path, jdk, monkeypatch
):
    _, _, repo = service
    wrapper(repo, "gradle")
    monkeypatch.setattr(java, "host_cache", lambda _: tmp_path / "host")
    calls = []
    mock_tools(monkeypatch, calls, fails=True)
    cfg = java_config("gradle")
    with pytest.raises(PipelineError, match="bootstrap failed"):
        prepare_dependencies(repo, cfg, tmp_path / "preflight")
    with pytest.raises(StopRequested):
        java.prepare_java(repo, cfg, tmp_path / "preflight", lambda: "pause")


@pytest.mark.parametrize("tool", ["maven", "gradle"])
def test_submodules_use_committed_parent_wrapper(service, tool):
    _, _, repo = service
    wrapper(repo, tool)
    manifest = "pom.xml" if tool == "maven" else "build.gradle.kts"
    commit_files(
        repo, {"service/" + manifest: "<project/>" if tool == "maven" else "plugins { java }"}
    )
    found = detect(repo)
    assert any(
        c.command.working_directory == "service"
        and c.command.argv[0] == ("../mvnw" if tool == "maven" else "../gradlew")
        for c in found.candidates
    )
