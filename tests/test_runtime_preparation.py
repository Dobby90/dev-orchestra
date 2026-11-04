"""Regression checks for real-project preparation, immutable source and test evidence."""

import base64
import hashlib
import io
import json
import platform
import sys
import tarfile

import pytest
from test_validation_policy import commit_files, detect
from test_workflow import service as service

from dev_orchestra.config import TestCommand as Command
from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure import artifacts, java_native
from dev_orchestra.infrastructure.java import java_argv, java_tool
from dev_orchestra.infrastructure.process import redact
from dev_orchestra.infrastructure.runtime_adapters import convert_jest_report, runtime_command
from dev_orchestra.infrastructure.test_runner import parse_junit, sandbox_command


def sri(data, algorithm="sha512"):
    return algorithm + "-" + base64.b64encode(hashlib.new(algorithm, data).digest()).decode()


@pytest.mark.parametrize(
    "url",
    [
        "http://registry.npmjs.org/tool.tgz",
        "https://private.invalid/tool.tgz",
        "https://user:password@registry.npmjs.org/tool.tgz",
        "https://registry.npmjs.org:8443/tool.tgz",
        "https://registry.npmjs.org/tool.tgz?token=secret",
        "https://codeload.github.com/org/tool/tar.gz/main",
    ],
)
def test_downloads_reject_nonpublic_unpinned_or_authenticated_urls(url):
    with pytest.raises(PipelineError):
        artifacts.public_url(url)


def test_download_checksum_cache_and_cancellation(tmp_path, monkeypatch):
    data = b"pinned package"
    url = "https://registry.npmjs.org/tool/-/tool-1.0.0.tgz"
    calls = []
    monkeypatch.setattr(artifacts, "fetch", lambda *args: calls.append(args[0]) or data)
    path = artifacts.verified(url, sri(data), tmp_path)
    assert path.read_bytes() == data
    assert artifacts.verified(url, sri(data), tmp_path) == path and len(calls) == 1
    path.write_bytes(b"corrupted cache")
    assert artifacts.verified(url, sri(data), tmp_path).read_bytes() == data
    with pytest.raises(PipelineError, match="checksum mismatch"):
        artifacts.verified(url, sri(b"different"), tmp_path)
    with pytest.raises(StopRequested):
        artifacts.verified(url, sri(data), tmp_path, lambda: "cancel")


def test_download_budget_and_groovy_path_literals():
    budget = artifacts.DownloadBudget(limit=8)
    budget.consume(8)
    with pytest.raises(PipelineError, match="total safety limit"):
        budget.consume(1)
    literal = java_native.groovy_literal("/tmp/${untrusted}/'quoted'")
    assert literal.startswith("'") and literal.endswith("'") and "\\'quoted\\'" in literal


def test_missing_public_npm_cache_is_imported_offline_without_project_scripts(
    tmp_path, monkeypatch
):
    from dev_orchestra.infrastructure import node_dependencies

    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    worktree = tmp_path / "project"
    worktree.mkdir()
    directory = tmp_path / "preflight"
    directory.mkdir()
    archive = directory / "verified.tgz"
    archive.write_bytes(b"verified artifact")
    url = "https://registry.npmjs.org/tool/-/tool-1.0.0.tgz"
    integrity = sri(archive.read_bytes())
    fetched, executed = [], []
    monkeypatch.setattr(
        node_dependencies,
        "fetch_entries",
        lambda entries, *args: fetched.extend(entries) or [archive],
    )
    monkeypatch.setattr(
        node_dependencies,
        "execute",
        lambda argv, *args, **kwargs: executed.append(argv) or (0, "ok"),
    )
    node_dependencies.prepare_npm_cache(
        worktree,
        {
            "packages": {
                "node_modules/tool": {"resolved": url, "integrity": integrity},
                "node_modules/private": {
                    "resolved": "https://private.invalid/tool.tgz",
                    "integrity": integrity,
                },
            }
        },
        directory,
        lambda: None,
    )
    assert fetched == [(url, integrity)]
    assert len(executed) == 1 and "--offline" in executed[0] and "--ignore-scripts" in executed[0]
    assert str(archive) in executed[0] and url not in executed[0]


def test_redirect_checked_before_opening_destination():
    with pytest.raises(PipelineError):
        artifacts.PublicRedirect().redirect_request(
            None, None, 302, "", {}, "https://private.invalid/"
        )


def test_yarn_classic_scopes_sha1_and_git_pins():
    commit = "a" * 40
    entries = artifacts.yarn_entries(f"""# yarn lockfile v1
"@scope/tool@1":
  version "1.0.0"
  resolved "https://registry.yarnpkg.com/@scope/tool/-/tool-1.0.0.tgz#{commit}"

"git-tool@https://github.com/org/tool.git#{commit}":
  version "1.0.0"
  resolved "https://github.com/org/tool.git#{commit}"
""")
    assert entries[0][2] == "@scope-tool-1.0.0.tgz"
    assert entries[0][1] == "sha1-" + base64.b64encode(bytes.fromhex(commit)).decode()
    assert entries[1][2] == "tool.git-" + commit
    with pytest.raises(PipelineError):
        artifacts.yarn_entries("__metadata:\n  version: 8")
    with pytest.raises(PipelineError):
        artifacts.yarn_entries('tool@1:\n  resolved "https://github.com/org/tool.git#main"\n')


def test_github_archive_checks_commit_and_never_extracts_paths(tmp_path, monkeypatch):
    commit = "a" * 40
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers={"comment": commit}
    ) as archive:
        item = tarfile.TarInfo("tool-commit/package.json")
        content = b'{"name":"tool"}'
        item.size = len(content)
        archive.addfile(item, io.BytesIO(content))
    monkeypatch.setattr(artifacts, "fetch", lambda *args: buffer.getvalue())
    url = "https://codeload.github.com/org/tool/tar.gz/" + commit
    path = artifacts.github_archive(url, "git-" + commit, tmp_path, lambda: None)
    with tarfile.open(path) as archive:
        assert archive.getnames() == ["package.json"]
    with pytest.raises(PipelineError, match="commit"):
        artifacts.github_archive(url, "git-" + "b" * 40, tmp_path, lambda: None)
    assert not (tmp_path / "package.json").exists()


def test_wrapper_without_executable_mode_uses_interpreter_without_chmod(tmp_path):
    wrapper = tmp_path / "gradlew"
    wrapper.write_text("#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o644)
    command = Command(argv=["./gradlew", "test"], junit_report="build/test.xml")
    assert java_argv(tmp_path, command) == ["/bin/sh", "./gradlew", "test"]
    assert command.argv == ["./gradlew", "test"] and wrapper.stat().st_mode & 0o777 == 0o644
    assert (
        java_tool(Command(argv=["/bin/sh", "./gradlew", "test"], junit_report="build/test.xml"))
        == "gradle"
    )
    assert (
        java_tool(Command(argv=["/bin/sh", "-c", "./gradlew test"], junit_report="build/test.xml"))
        is None
    )


def test_detect_jest_lint_fix_vue_cache_and_gradle_root(service):
    _, _, repo = service
    commit_files(
        repo,
        {
            "package.json": json.dumps(
                {
                    "scripts": {
                        "test": "jest",
                        "lint": "vue-cli-service lint --fix",
                        "build": "vue-cli-service build",
                    },
                    "devDependencies": {"jest": "26.6.3"},
                }
            ),
            "settings.gradle": "include('api')",
            "api/build.gradle": "plugins { id 'java' }",
        },
    )
    found = detect(repo)
    assert not any("lint" in c.command.argv for c in found.candidates)
    jest = next(c for c in found.candidates if c.label == ". · test")
    assert "--json" in jest.command.argv and not jest.warnings
    build = next(c for c in found.candidates if c.label == ". · build")
    assert "node_modules/.cache" in build.output_paths
    gradle = next(c for c in found.candidates if c.label == "api · Gradle 테스트")
    assert "build" in gradle.output_paths


def test_existing_vue_policy_is_not_silently_expanded(service, tmp_path):
    from test_dependencies import package

    from dev_orchestra.infrastructure.dependencies import prepare_dependencies

    _, _, repo = service
    config = package(repo, scripts={"build": "vue-cli-service build"})
    config.commands["build"] = Command(kind="check", argv=["npm", "run", "build"])
    before = config.model_dump()
    with pytest.raises(PipelineError, match=r"node_modules/\.cache.*new run"):
        prepare_dependencies(repo, config, tmp_path / "preflight")
    assert config.model_dump() == before and not (repo / "node_modules").exists()


def test_jest_conversion_preserves_pass_fail_skip_and_rejects_summary_forgery(tmp_path):
    source, target = tmp_path / "jest.json", tmp_path / "junit.xml"
    report = {
        "numPassedTests": 1,
        "numFailedTests": 1,
        "numPendingTests": 1,
        "numTotalTests": 3,
        "testResults": [
            {
                "name": "real.test.js",
                "assertionResults": [
                    {"fullName": "pass", "status": "passed"},
                    {
                        "fullName": "fail",
                        "status": "failed",
                        "failureMessages": ["Expected 2, received 3"],
                    },
                    {"fullName": "skip", "status": "pending"},
                ],
            }
        ],
    }
    source.write_text(json.dumps(report))
    convert_jest_report(source, target)
    parsed = parse_junit(target)
    assert (parsed["total"], parsed["failed"], parsed["skipped"]) == (3, 1, 1)
    report["numFailedTests"] = 0
    source.write_text(json.dumps(report))
    with pytest.raises(PipelineError, match="totals"):
        convert_jest_report(source, target)
    source.write_text("{}")
    with pytest.raises(PipelineError, match="invalid Jest"):
        convert_jest_report(source, target)


def test_gradle_verification_metadata_cannot_be_bypassed(service):
    _, _, repo = service
    commit_files(
        repo,
        {
            "gradle/verification-metadata.xml": "<verification-metadata><components/></verification-metadata>"
        },
    )
    with pytest.raises(PipelineError, match="matching checksum"):
        java_native.project_checksum(repo, "com.google.protobuf", "protoc", "3.24.4", "protoc.exe")


def test_mislabeled_native_compiler_blocks_build_before_model_work(tmp_path, monkeypatch):
    from dev_orchestra.infrastructure import process

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    monkeypatch.setattr(platform, "machine", lambda: "arm64")
    target = tmp_path / "protoc-gen-grpc-java-osx-aarch_64.exe"
    target.write_bytes(bytes.fromhex("cffaedfe07000001"))
    calls = []
    monkeypatch.setattr(
        process, "execute", lambda *args, **kwargs: calls.append(args) or (1, "Bad CPU type")
    )
    unit = Command(argv=["./gradlew", ":agent:test"], junit_report="build/test.xml")
    java_native.check_native_cpu(tmp_path, unit, "controller", target, tmp_path, lambda: None)
    assert not calls
    build = Command(kind="check", argv=["./gradlew", "assemble"])
    with pytest.raises(PipelineError, match="Rosetta is unavailable"):
        java_native.check_native_cpu(tmp_path, build, "controller", target, tmp_path, lambda: None)
    assert len(calls) == 1


def test_native_cache_marker_symlink_cannot_overwrite_external_file(service, tmp_path):
    from test_java_preparation import java_config

    _, _, repo = service
    commit_files(
        repo,
        {"build.gradle": "protobuf { protoc { artifact = 'com.google.protobuf:protoc:3.24.4' } }"},
    )
    command = java_config("gradle").commands["java"]
    root = java_native.tool_root(repo, command)
    root.mkdir(parents=True)
    external = tmp_path / "protected.json"
    external.write_text("protected")
    (root / f"protoc-3.24.4-{java_native.classifier()}.json").symlink_to(external)
    with pytest.raises(PipelineError):
        java_native.prepare_native_tools(repo, command, tmp_path / "preflight", lambda: None)
    assert external.read_text() == "protected"


def test_jest_global_failure_never_passes_when_assertions_pass(tmp_path):
    source, target = tmp_path / "jest.json", tmp_path / "junit.xml"
    source.write_text(
        json.dumps(
            {
                "success": False,
                "numPassedTests": 1,
                "numFailedTests": 0,
                "numPendingTests": 0,
                "numTotalTests": 1,
                "testResults": [
                    {
                        "name": "suite.test.js",
                        "status": "failed",
                        "message": "afterAll failed",
                        "assertionResults": [{"fullName": "passed assertion", "status": "passed"}],
                    }
                ],
            }
        )
    )
    convert_jest_report(source, target)
    with pytest.raises(PipelineError):
        parse_junit(target)


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS sandbox profile")
def test_iokit_exception_applies_only_to_explicit_browser_policy(tmp_path):
    normal = sandbox_command(tmp_path, [], tmp_path, [sys.executable], "loopback")[2]
    browser = sandbox_command(tmp_path, [], tmp_path, [sys.executable], "loopback", browser=True)[2]
    assert "iokit-open" not in normal
    assert '(allow iokit-open (iokit-user-client-class "RootDomainUserClient"))' in browser
    assert "(allow iokit-open)" not in browser
    assert '(remote ip "localhost:*")' in browser and "(deny default)" in browser


def test_vue_runtime_hook_preserves_policy_and_source(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "vue-cli-service build"}})
    )
    command = Command(kind="check", argv=["yarn", "run", "build"])
    before = command.model_dump()
    env = {"NODE_OPTIONS": "--max-old-space-size=2048"}
    argv, evidence = runtime_command(tmp_path, command, tmp_path, env)
    assert argv == command.argv and command.model_dump() == before
    assert "--max-old-space-size=2048" in env["NODE_OPTIONS"] and "--require" in env["NODE_OPTIONS"]
    assert evidence[0]["adapter"] == "vue-eslint-readonly"


def test_gradle_groovy_credentials_and_url_auth_are_redacted():
    text = redact(
        "password \"private-value\"\nusername 'private-user'\nusername = 'private-user'\nHTTPS://private-user:private-value@example.invalid/repo"
    )
    assert "private-value" not in text and "private-user" not in text
