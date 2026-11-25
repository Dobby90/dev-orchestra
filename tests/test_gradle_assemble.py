"""Keep selected checks and JUnit evidence intact across Gradle batching."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from test_automatic_java_execution import local_tool
from test_validation_policy import commit_files
from test_workflow import service as service

from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as Verification
from dev_orchestra.infrastructure import coordination, java, test_runner
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.java_cache import GradleCacheBindings, cache_identity


def prepare_seed(repo, preflight, checks):
    seed = preflight / "gradle-seeds" / ("a" * 64)
    (seed / "caches").mkdir(parents=True)
    reference = {
        "seed": seed.name,
        "stamp": cache_identity([seed], lambda: None),
        "group": "b" * 64,
    }
    GradleCacheBindings(preflight, repo).publish({key: reference for key in checks})


@pytest.fixture
def verification(service, tmp_path, monkeypatch):
    _, _, repo = service
    monkeypatch.setattr(java, "java_environment", lambda *args: {"JAVA_HOME": "/tmp/fake-jdk"})
    commit_files(
        repo,
        {
            "gradlew": "#!/bin/sh\nexit 1\n",
            "settings.gradle": "include 'bad', 'good'; project(':good').projectDir = file('physical')",
            "bad/build.gradle": "apply plugin: 'base'\n",
            "physical/build.gradle": "apply plugin: 'base'\n",
        },
    )
    checks = {
        key: Command(
            kind="check",
            working_directory=cwd,
            argv=[
                "../gradlew",
                "--offline",
                "--no-daemon",
                "--gradle-user-home",
                ".devorchestra-cache/gradle",
                "--project-cache-dir",
                ".devorchestra-cache/project",
                "assemble",
                java.GRADLE_DIRECTORY_TASK + "assemble",
            ],
        )
        for key, cwd in [("bad_build", "bad"), ("good_build", "physical")]
    }
    unit = Command(
        argv=[
            sys.executable,
            "-c",
            "from pathlib import Path; Path('build/unit.xml').write_text('<testsuite><testcase name=\"unit\"/></testsuite>')",
        ],
        junit_report="build/unit.xml",
    )
    commands = Verification(
        commands=checks | {"unit": unit},
        output_paths=["build", "bad/.devorchestra-cache", "physical/.devorchestra-cache"],
    )
    preflight = tmp_path / "preflight"
    prepare_seed(repo, preflight, checks)
    return repo, commands, preflight


@pytest.mark.parametrize("mode", ["batch", "missing_evidence", "incompatible", "private_cache"])
def test_batch_fallback_and_command_evidence(verification, tmp_path, monkeypatch, mode):
    repo, config, preflight = verification
    calls = []
    monkeypatch.setattr(test_runner, "sandbox_command", lambda _w, _o, _s, argv, _network: argv)

    def execute(argv, cwd, timeout, log, **kwargs):
        calls.append((argv, cwd, timeout))
        if log.name == "unit.log":
            (repo / "build/unit.xml").write_text('<testsuite><testcase name="unit"/></testsuite>')
            return 0, ""
        if not log.name.startswith("gradle-assemble-"):
            return (1, "fixture failed") if cwd.name == "bad" else (0, "")
        if mode == "missing_evidence":
            return 0, "BUILD SUCCESSFUL\n"
        marker = re.search(r"DEVORCHESTRA_ASSEMBLE_[a-f0-9]+ ", Path(argv[-1]).read_text())[0]
        rows = [
            {
                "targets": {
                    "bad_build": {
                        "task": ":bad:assemble",
                        "dependencies": [":bad:assemble", ":bad:compile"],
                    },
                    "good_build": {"task": ":good:assemble", "dependencies": [":good:assemble"]},
                }
            },
            {"task": ":bad:compile", "failed": True},
            {"task": ":good:assemble", "failed": False},
        ]
        return 1, "\n".join(marker + json.dumps(row) for row in rows)

    if mode == "incompatible":
        config.commands["bad_build"].timeout_seconds = 110
    if mode == "private_cache":
        cache = repo / "physical/.devorchestra-cache/gradle/caches/modules-2/files-2.1"
        cache.mkdir(parents=True)
        (cache / "private.jar").touch()
    monkeypatch.setattr(test_runner, "execute", execute)
    directory = tmp_path / "evidence"
    result = test_runner.TestRunner(preflight=preflight).run(repo, config, directory)
    assert result["status"] == "failed"
    bad, good, unit = result["commands"]
    assert [row["command_id"] for row in (bad, good, unit)] == list(config.commands)
    assert [row["status"] for row in (bad, good, unit)] == ["failed", "passed", "passed"]
    assert unit["total"] == 1 and unit["junit_hash"] == digest(
        Path(unit["junit_path"]).read_bytes()
    )
    assert all(row["argv"] == config.commands[row["command_id"]].argv for row in (bad, good, unit))
    observation = json.loads(next(directory.glob("test-invocation-*.json")).read_text())
    if mode == "batch":
        assert len(calls) == 2 and "--continue" in calls[0][0]
        assert bad["runtime_batch"]["task"] == ":bad:assemble"
        assert good["runtime_batch"]["task"] == ":good:assemble"
        assert ":bad:compile" in bad["error"]
        assert (
            result["duration_seconds"]
            == observation["batches"][0]["duration_seconds"] + unit["duration_seconds"]
        )
    elif mode == "missing_evidence":
        assert len(calls) == 4 and not observation["batches"][0]["used"]
    else:
        assert len(calls) == 3 and "batches" not in observation


@pytest.mark.skipif(
    os.environ.get("DEVORCHESTRA_TEST_SANDBOX") != "1", reason="Local Gradle and OS sandbox check"
)
def test_real_gradle_assemble_reports_failure_and_continues(verification, tmp_path, monkeypatch):
    from dev_orchestra.infrastructure import gradle_assemble

    repo, config, preflight = verification
    jdk = java.select_jdk(17)
    if not jdk:
        pytest.skip("Local JDK 17 required")
    gradle = local_tool("gradle")
    monkeypatch.setattr(java, "java_environment", lambda *args: {"JAVA_HOME": str(jdk)})
    monkeypatch.setattr(coordination, "host_root", lambda: tmp_path / "slots")
    commit_files(
        repo,
        {
            "gradlew": f'#!/bin/sh\nexec {shlex.quote(str(gradle))} "$@"\n',
            "bad/build.gradle": "plugins { id 'base' }; tasks.register('failCompile') { doLast { throw new GradleException('fixture') } }; assemble.dependsOn('failCompile')",
        },
    )
    for command in config.commands.values():
        if command.kind == "check":
            command.network = "loopback"
    standalone = tmp_path / "standalone-repo"
    subprocess.run(["git", "clone", "--quiet", str(repo), str(standalone)], check=True)
    standalone_preflight = tmp_path / "standalone-preflight"
    prepare_seed(
        standalone,
        standalone_preflight,
        (key for key, command in config.commands.items() if command.kind == "check"),
    )
    with monkeypatch.context() as isolated:
        isolated.setattr(gradle_assemble, "assemble_batches", lambda *args: {})
        expected = test_runner.TestRunner(preflight=standalone_preflight).run(
            standalone, config, tmp_path / "individual"
        )
    runner = test_runner.TestRunner(preflight=preflight)
    result = runner.run(repo, config, tmp_path / "batched")
    bad, good, unit = result["commands"]
    assert [row["status"] for row in expected["commands"]] == ["failed", "passed", "passed"], (
        expected
    )
    assert [
        (row["command_id"], row["status"], row["junit_hash"]) for row in result["commands"]
    ] == [(row["command_id"], row["status"], row["junit_hash"]) for row in expected["commands"]], (
        result
    )
    assert bad["runtime_batch"]["task"] == ":bad:assemble" and ":bad:failCompile" in bad["error"]
    assert not bad["runtime_batch"]["task_completed"]
    assert good["runtime_batch"]["task"] == ":good:assemble"
    assert good["runtime_batch"]["task_completed"]
    assert unit["total"] == 1
