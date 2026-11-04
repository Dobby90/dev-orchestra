"""Dependency failures must precede model work and preserve isolation and source."""

import json
import shutil
from pathlib import Path

import pytest
from test_validation_policy import commit_files
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.application.service import RunService
from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as Commands
from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure import dependencies
from dev_orchestra.infrastructure.dependencies import prepare_dependencies
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.test_runner import TestRunner
from dev_orchestra.orchestration.runner import Runner


def package(repo, **overrides):
    manifest = {"name": "fixture", "version": "1.0.0", "devDependencies": {"tool": "1.0.0"}}
    manifest.update(overrides)
    commit_files(
        repo,
        {
            "package.json": json.dumps(manifest),
            "package-lock.json": json.dumps({"lockfileVersion": 3, "packages": {"": manifest}}),
            ".gitignore": "node_modules/\n",
        },
    )
    return Commands(
        commands={"node-test": Command(argv=["npm", "test"], junit_report="build/test.xml")}
    )


def test_missing_lock_stops_before_any_model_work(service):
    svc, rid, repo = service
    package(repo)
    (repo / "package-lock.json").unlink()
    Git(repo).commit("잠금 파일 누락 검증")
    # Create a fresh run against the committed npm fixture.
    cfg = svc.config.model_copy(deep=True)
    cfg.tests = Commands(
        commands={"unit": Command(argv=["npm", "test"], junit_report="build/test.xml")}
    )
    svc = RunService(cfg)
    rid = svc.create(repo, repo / "package.json")
    agent = FakeAgent()
    Runner(svc.store, agent, TestRunner())._execute(rid)
    result = svc.store.get(rid)
    assert result["state"] == "needs_input" and not agent.calls
    assert "unit" in result["error"] and "package-lock.json" in result["error"]
    assert not svc.store.steps(rid)


def test_offline_install_retries_and_refreshes_only_managed_dependencies(
    service, tmp_path, monkeypatch
):
    _, _, repo = service
    cfg = package(repo)
    before = (repo / "main.py").read_text()
    commands = []
    fail = True

    def sandbox(worktree, outputs, scratch, argv):
        assert worktree == repo
        assert outputs == ([repo / "node_modules"] if argv[1] == "ci" else [])
        assert "--offline" in argv and "--ignore-scripts" in argv and "--include=dev" in argv
        return argv

    def execute(argv, cwd, timeout, log, **kwargs):
        commands.append(argv[1])
        assert cwd == repo and kwargs["env"]["HOME"] != str(Path.home())
        return (1, "cache missing token=private") if fail else (0, "ok")

    monkeypatch.setattr(dependencies, "sandbox_command", sandbox)
    monkeypatch.setattr(dependencies, "execute", execute)
    directory = tmp_path / "preflight"
    with pytest.raises(PipelineError, match="offline npm ci") as error:
        prepare_dependencies(repo, cfg, directory)
    assert "private" not in str(error.value)
    fail = False
    prepare_dependencies(repo, cfg, directory)
    prepare_dependencies(repo, cfg, directory)
    assert commands == ["ci", "ci", "ls", "ls"]
    (repo / "package.json").write_text((repo / "package.json").read_text() + "\n")
    prepare_dependencies(repo, cfg, directory)
    assert commands[-2:] == ["ci", "ls"]
    assert (repo / "main.py").read_text() == before


@pytest.mark.parametrize("unsafe", ["tracked", "symlink", "escape"])
def test_dependency_writes_never_include_source_or_external_paths(
    service, tmp_path, monkeypatch, unsafe
):
    _, _, repo = service
    cfg = package(repo)
    if unsafe == "tracked":
        commit_files(repo, {"node_modules/protected.txt": "source"})
        Git(repo).call("add", "-f", "node_modules/protected.txt")
        Git(repo).call("commit", "-m", "추적 소스 보호 검증")
    elif unsafe == "symlink":
        (repo / "node_modules").symlink_to(tmp_path, target_is_directory=True)
    else:
        (repo / "package-lock.json").write_text(json.dumps({"packages": {"../outside": {}}}))

    def forbidden(*args, **kwargs):
        pytest.fail("unsafe dependency layout must never execute npm")

    monkeypatch.setattr(dependencies, "execute", forbidden)
    with pytest.raises(PipelineError):
        prepare_dependencies(repo, cfg, tmp_path / "preflight")


def test_dependency_cancellation_is_not_converted_to_needs_input(service, tmp_path, monkeypatch):
    _, _, repo = service
    cfg = package(repo)

    def cancelled(*args, **kwargs):
        raise StopRequested("cancel")

    monkeypatch.setattr(dependencies, "execute", cancelled)
    with pytest.raises(StopRequested):
        prepare_dependencies(repo, cfg, tmp_path / "preflight")


def test_manually_prepared_dependencies_are_checked_without_reinstall(
    service, tmp_path, monkeypatch
):
    _, _, repo = service
    cfg = package(repo)
    modules = repo / "node_modules"
    modules.mkdir()
    sentinel = modules / "manual"
    sentinel.write_text("preserve")

    def execute(argv, *args, **kwargs):
        assert "ls" in argv and "ci" not in argv
        return 1, "UNMET DEPENDENCY tool@1.0.0"

    monkeypatch.setattr(dependencies, "execute", execute)
    with pytest.raises(PipelineError, match="UNMET DEPENDENCY"):
        prepare_dependencies(repo, cfg, tmp_path / "preflight")
    assert sentinel.read_text() == "preserve"


def test_vite_output_policy_is_checked_without_expanding_existing_run_permissions(
    service, tmp_path
):
    _, _, repo = service
    cfg = package(repo, scripts={"build": "tsc --noEmit && vite build"})
    commit_files(repo, {"vite.config.ts": "export default {};"})
    cfg.commands["build"] = Command(kind="check", argv=["npm", "run", "build"])
    before = cfg.model_dump()
    with pytest.raises(PipelineError, match=r"node_modules/\.vite-temp.*new run"):
        prepare_dependencies(repo, cfg, tmp_path / "preflight")
    assert cfg.model_dump() == before and not (repo / "node_modules").exists()


def test_runner_preserves_pause_during_dependency_preparation(service):
    svc, rid, _ = service

    class Paused(FakeTests):
        def prepare(self, *args):
            raise StopRequested("pause")

    agent = FakeAgent()
    Runner(svc.store, agent, Paused()).execute(rid)
    assert svc.store.get(rid)["state"] == "paused" and not agent.calls


def test_missing_configured_browser_cache_is_reported_before_model_work(service, tmp_path):
    svc, rid, _ = service
    cfg = svc.config.model_copy(deep=True)
    cfg.tests.commands["unit"].env["PLAYWRIGHT_BROWSERS_PATH"] = str(tmp_path / "missing-browsers")
    svc.store.update(rid, config=cfg.model_dump(mode="json"))
    agent = FakeAgent()
    Runner(svc.store, agent, TestRunner())._execute(rid)
    result = svc.store.get(rid)
    assert result["state"] == "needs_input" and not agent.calls
    assert "unit" in result["error"] and "PLAYWRIGHT_BROWSERS_PATH" in result["error"]


@pytest.mark.skipif(not shutil.which("npm"), reason="npm required")
def test_real_offline_workspace_install_disables_lifecycle_scripts(service, tmp_path):
    _, _, repo = service
    manifest = {
        "name": "fixture",
        "version": "1.0.0",
        "workspaces": ["packages/tool"],
        "scripts": {
            "postinstall": "node -e \"require('fs').writeFileSync('node_modules/UNSAFE','bad')\""
        },
    }
    workspace = {"name": "local-tool", "version": "1.0.0"}
    commit_files(
        repo,
        {
            "package.json": json.dumps(manifest),
            "packages/tool/package.json": json.dumps(workspace),
            "package-lock.json": json.dumps(
                {
                    "lockfileVersion": 3,
                    "packages": {
                        "": manifest,
                        "packages/tool": workspace,
                        "node_modules/local-tool": {"resolved": "packages/tool", "link": True},
                    },
                }
            ),
            ".gitignore": "node_modules/\n",
        },
    )
    cfg = Commands(
        commands={
            "unit": Command(
                argv=["npm", "test"],
                working_directory="packages/tool",
                junit_report="build/test.xml",
            )
        }
    )
    before = Git(repo).dirty()
    prepare_dependencies(repo, cfg, tmp_path / "preflight")
    assert (repo / "node_modules/local-tool/package.json").is_file()
    assert not (repo / "node_modules/UNSAFE").exists()
    assert Git(repo).dirty() == before
