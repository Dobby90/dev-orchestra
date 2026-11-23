"""Java readiness must precede model work and preserve source, policy and host caches."""

import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_validation_policy import commit_files, detect
from test_workflow import FakeAgent
from test_workflow import service as service

from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as Commands
from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure import java, java_cache, java_native, test_runner
from dev_orchestra.infrastructure.dependencies import prepare_dependencies
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.gradle_preparation import ModuleProgress, observe_preparation
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


@pytest.fixture
def gradle_pair(service, tmp_path, jdk, monkeypatch):
    _, _, repo = service
    wrapper(repo, "gradle")
    cfg = java_config("gradle")
    test = cfg.commands["java"]
    test.network = "none"
    cfg.commands["build"] = Command(
        kind="check", argv=["assemble" if arg == "test" else arg for arg in test.argv]
    )
    monkeypatch.setattr(java, "host_cache", lambda _: tmp_path / "host")
    return repo, cfg


def test_gradle_pair_reuses_successful_preparation_and_executes_both_commands(
    gradle_pair, tmp_path, monkeypatch
):
    repo, cfg = gradle_pair
    commit_files(
        repo,
        {
            "gradlew": """#!/bin/sh
case "$1" in
  test) echo '<testsuite><testcase name="unit"/></testsuite>' > build/unit.xml ;;
  assemble) exit 7 ;;
  *) exit 2 ;;
esac
"""
        },
    )
    probes = []
    mock_tools(monkeypatch, probes)
    runner = TestRunner()
    progress = []
    with observe_preparation(progress.append):
        runner.prepare(repo, cfg, tmp_path / "preflight")
        assert progress[-1] == dict(total=1, checked=1, reused=0, active=True)
        TestRunner().prepare(repo, cfg, tmp_path / "preflight")
        assert progress[-1] == dict(total=1, checked=0, reused=1, active=True)
    assert [argv[-1] for argv, _ in probes] == ["-version", "--version"]

    monkeypatch.setattr(test_runner, "sandbox_command", lambda w, o, s, argv, network: argv)
    results = runner.run(repo, cfg, tmp_path / "results")["commands"]
    assert [(r["command_id"], r["exit_code"]) for r in results] == [("java", 0), ("build", 7)]
    assert results[0]["passed"] and results[0]["junit_hash"]
    assert not results[1]["passed"] and results[1]["junit_path"] is None


def test_gradle_modules_share_private_seed_and_only_recheck_changed_cache(
    service, tmp_path, jdk, monkeypatch
):
    _, _, repo = service
    wrapper(repo, "gradle")
    commit_files(
        repo,
        {
            "settings.gradle": 'includeBuild("build-logic")\ninclude("a", "b")\n',
            "a/build.gradle": "",
            "b/build.gradle": "",
            "build-logic/build.gradle": "",
        },
    )
    cfg = Commands()
    for name in ("a", "b", "build-logic"):
        module = java_config("gradle", directory=name)
        cfg.output_paths.extend(module.output_paths)
        command = module.commands["java"]
        command.argv[0] = "../gradlew"
        cfg.commands[name] = command
    artifact = Path("caches/modules-2/files-2.1/org/lib/1/hash/lib.jar")
    host = tmp_path / "host"
    (host / artifact).parent.mkdir(parents=True)
    (host / artifact).write_bytes(b"fixture")
    monkeypatch.setattr(java, "host_cache", lambda _: host)
    probes = []
    mock_tools(monkeypatch, probes)
    preflight = tmp_path / "preflight"
    progress = []
    with observe_preparation(progress.append):
        java.prepare_java(repo, cfg, preflight)
        assert progress[-1]["checked"] == 3
        seeds = list((preflight / "gradle-seeds").glob("*/" + str(artifact)))
        assert len(seeds) == 2
        copies = [repo / name / ".devorchestra-cache/gradle" / artifact for name in cfg.commands]
        assert len({f.stat().st_ino for f in [host / artifact, *seeds, *copies]}) == 6
        assert all(file.stat().st_mode & 0o222 == 0 for file in seeds)
        copies[0].unlink()
        java.prepare_java(repo, cfg, preflight)
    assert copies[0].read_bytes() == b"fixture"
    assert len(probes) == 8
    assert progress[-1] == dict(total=3, checked=1, reused=2, active=True)


def test_failed_gradle_check_does_not_issue_reusable_evidence(gradle_pair, tmp_path, monkeypatch):
    repo, cfg = gradle_pair
    preflight = tmp_path / "preflight"
    mock_tools(monkeypatch, [], fails=True)
    with pytest.raises(PipelineError, match="bootstrap failed"):
        java.prepare_java(repo, cfg, preflight)
    assert not list(preflight.glob("gradle-readiness/*.json"))
    probes = []
    mock_tools(monkeypatch, probes)
    java.prepare_java(repo, cfg, preflight)
    (repo / "gradlew").write_text("#!/bin/sh\nexit 1\n")
    java.prepare_java(repo, cfg, preflight)
    assert len(probes) == 4


def test_same_path_jdk_replacement_invalidates_readiness(gradle_pair, jdk, tmp_path, monkeypatch):
    repo, cfg = gradle_pair
    module = jdk / "lib/modules"
    module.parent.mkdir()
    module.write_bytes(b"before")
    probes = []
    mock_tools(monkeypatch, probes)
    preflight = tmp_path / "preflight"
    java.prepare_java(repo, cfg, preflight)
    original = module.stat()
    module.with_suffix(".new").write_bytes(b"change")
    module.with_suffix(".new").replace(module)
    os.utime(module, ns=(original.st_atime_ns, original.st_mtime_ns))
    java.prepare_java(repo, cfg, preflight)
    java.prepare_java(repo, cfg, preflight)
    assert len(probes) == 4


def test_installed_gradle_library_change_rechecks_launcher(gradle_pair, tmp_path, monkeypatch):
    repo, cfg = gradle_pair
    install = tmp_path / "gradle"
    (install / "bin").mkdir(parents=True)
    (install / "lib").mkdir()
    binary = install / "bin/gradle"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    library = install / "lib/gradle-launcher-8.jar"
    library.write_text("gradle")
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    for command in cfg.commands.values():
        command.argv[0] = "gradle"
    probes = []
    mock_tools(monkeypatch, probes)
    preflight = tmp_path / "preflight"
    java.prepare_java(repo, cfg, preflight)
    java.prepare_java(repo, cfg, preflight)
    library.write_text("updated")
    java.prepare_java(repo, cfg, preflight)
    assert len(probes) == 4


def test_cancel_during_seed_copy_can_retry_cleanly(gradle_pair, tmp_path, monkeypatch):
    repo, cfg = gradle_pair
    artifact = tmp_path / "host/caches/modules-2/lib.jar"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"fixture" * 200000)
    copy = java_cache.copy_artifact
    copying = False

    def interrupted_copy(*args, **kwargs):
        nonlocal copying
        copying = True
        return copy(*args, **kwargs)

    monkeypatch.setattr(java_cache, "copy_artifact", interrupted_copy)
    probes = []
    mock_tools(monkeypatch, probes)
    preflight = tmp_path / "preflight"
    with pytest.raises(StopRequested):
        java.prepare_java(repo, cfg, preflight, lambda: "cancel" if copying else None)
    assert not list((preflight / "gradle-seeds").iterdir())
    assert not list(preflight.glob("gradle-readiness/*.json"))
    assert not probes
    java.prepare_java(repo, cfg, preflight)
    assert len(probes) == 2
    assert (
        repo / ".devorchestra-cache/gradle/caches/modules-2/lib.jar"
    ).read_bytes() == artifact.read_bytes()


def test_resumed_preparation_reports_progress_and_polls_only_control(service, monkeypatch):
    svc, rid, _ = service
    store = svc.store
    store.update(rid, next_role="developer")
    run = store.get(rid)
    monkeypatch.setattr("dev_orchestra.orchestration.runner.monotonic", lambda: 100.0)

    class Preparing:
        def prepare(self, worktree, config, directory, stop):
            assert store.get(rid)["state"] == "preparing"
            modules = ModuleProgress(["a", "b"])
            version = store.get(rid)["version"]
            for _ in range(50):
                assert stop() is None
            modules.complete("a")
            modules.complete("b", checked=True)
            assert store.get(rid)["version"] == version
            assert store.get(rid)["gradle_preparation"]["reused"] == 1

    worker = Runner(store, None, Preparing())

    def resume(run_id):
        poll = Mock(wraps=store.read_control)
        monkeypatch.setattr(store, "read_control", poll)
        worker._prepare(
            run_id, Path(run["worktree"]), svc.config.tests, Path(run["directory"]) / "preflight"
        )
        assert poll.call_count == 2
        store.update(run_id, "developing")

    monkeypatch.setattr(worker, "_execute", resume)
    worker.execute(rid)
    assert store.get(rid)["state"] == "developing"
    assert not store.get(rid)["gradle_preparation"]["active"]


@pytest.mark.parametrize(
    "difference", ["jdk", "wrapper", "user_cache", "project_cache", "network", "cwd"]
)
def test_gradle_environment_differences_require_separate_probes(
    gradle_pair, jdk, tmp_path, monkeypatch, difference
):
    repo, cfg = gradle_pair
    build = cfg.commands["build"]
    if difference == "jdk":
        other = tmp_path / "jdk-alt"
        other.mkdir()
        (other / "bin").symlink_to(jdk / "bin", target_is_directory=True)
        build.env["JAVA_HOME"] = str(other)
    elif difference == "wrapper":
        commit_files(
            repo,
            {
                "other/gradlew": "#!/bin/sh\nexit 0\n",
                "other/gradle/wrapper/gradle-wrapper.properties": (
                    repo / "gradle/wrapper/gradle-wrapper.properties"
                ).read_text(),
            },
        )
        build.argv[0] = "./other/gradlew"
    elif difference in {"user_cache", "project_cache"}:
        option = "--gradle-user-home" if difference == "user_cache" else "--project-cache-dir"
        build.argv[build.argv.index(option) + 1] = ".devorchestra-cache/alternate"
    elif difference == "network":
        build.network = "loopback"
    else:
        build.working_directory = "module"
        (repo / "module").mkdir()
        build.argv[0] = "../gradlew"
        cfg.output_paths.append("module/.devorchestra-cache")
    probes = []
    mock_tools(monkeypatch, probes)
    java.prepare_java(repo, cfg, tmp_path / "preflight")
    assert [argv[-1] for argv, _ in probes] == ["-version", "--version"] * 2


def test_gradle_shared_environment_enforces_each_commands_policy(
    gradle_pair, tmp_path, monkeypatch
):
    repo, cfg = gradle_pair
    mock_tools(monkeypatch, [])
    cfg.commands["build"].argv.remove("--offline")
    with pytest.raises(PipelineError, match="build .*requires offline mode"):
        java.prepare_java(repo, cfg, tmp_path / "preflight")


def test_gradle_shared_environment_checks_native_tools_for_each_command(
    gradle_pair, tmp_path, monkeypatch
):
    repo, cfg = gradle_pair
    mock_tools(monkeypatch, [])
    entries = [("module", "com.google.protobuf", "protoc", "3.24.4")]
    discover = Mock(return_value=entries)
    native = Mock(side_effect=[None, PipelineError("native compiler unavailable for build")])
    monkeypatch.setattr(java_native, "declarations", discover)
    monkeypatch.setattr(java_native, "prepare_native_tools", native)
    with pytest.raises(PipelineError, match="native compiler unavailable for build"):
        java.prepare_java(repo, cfg, tmp_path / "preflight")
    discover.assert_called_once_with(repo)
    assert [call.args[1] for call in native.call_args_list] == list(cfg.commands.values())
    assert all(call.kwargs["entries"] == entries for call in native.call_args_list)


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
