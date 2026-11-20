"""Exact Node pins must reach Python bootstrap descendants without shell initialization."""

import json
import os
import sys
from pathlib import Path

import pytest
from test_dependencies import package
from test_workflow import FakeAgent
from test_workflow import service as service

from dev_orchestra.config import TestCommand as Command
from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure import node_runtime
from dev_orchestra.infrastructure.dependencies import prepare_dependencies
from dev_orchestra.infrastructure.node_runtime import node_environment, node_pin, node_project
from dev_orchestra.infrastructure.test_runner import TestRunner
from dev_orchestra.orchestration.runner import Runner


def bootstrap():
    return Command(
        argv=[sys.executable, str(Path(node_runtime.__file__).with_name("unittest_bootstrap.py"))],
        junit_report="build/junit.xml",
    )


def test_python_bootstrap_discovers_node_project_but_unrelated_python_does_not(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert node_project(tmp_path, bootstrap()) == tmp_path
    unrelated = bootstrap().model_copy(update={"argv": [sys.executable, "custom.py"]})
    assert node_project(tmp_path, unrelated) is None
    (tmp_path / "package.json").unlink()
    assert node_project(tmp_path, bootstrap()) is None


def test_nested_command_uses_workspace_root(tmp_path):
    nested = tmp_path / "apps/web"
    nested.mkdir(parents=True)
    for root in (tmp_path, nested):
        (root / "package.json").write_text("{}")
    (tmp_path / "pnpm-workspace.yaml").write_text('packages: ["apps/*"]')
    command = Command(
        argv=["node", "test.js"], working_directory="apps/web", junit_report="build/junit.xml"
    )
    assert node_project(tmp_path, command) == tmp_path


def test_exact_pin_conflicts_are_rejected_and_ranges_keep_existing_behavior(tmp_path):
    (tmp_path / "package.json").write_text('{"engines":{"node":">=22"}}')
    assert node_pin(tmp_path) is None
    (tmp_path / ".node-version").write_text("24.12.0\n")
    (tmp_path / ".nvmrc").write_text("v24.12.0\n")
    assert node_pin(tmp_path) == "24.12.0"
    (tmp_path / "package.json").write_text('{"engines":{"node":"22.18.0"}}')
    with pytest.raises(PipelineError, match="conflicting exact Node versions"):
        node_pin(tmp_path)


def test_pin_cannot_follow_a_symlink(tmp_path):
    (tmp_path / "version").write_text("24.12.0")
    (tmp_path / ".node-version").symlink_to(tmp_path / "version")
    with pytest.raises(PipelineError, match="symlink"):
        node_pin(tmp_path)


def runtime_fixture(tmp_path, monkeypatch, *, fnm=True, version="22.18.0"):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".node-version").write_text("24.12.0")
    host = tmp_path / "host/node"
    pinned = tmp_path / "fnm/installation/bin/node"
    for binary in (host, pinned):
        binary.parent.mkdir(parents=True)
        binary.touch()
    monkeypatch.setattr(
        node_runtime, "environment", lambda: {"PATH": str(host.parent), "HOME": str(tmp_path)}
    )
    monkeypatch.setattr(
        node_runtime.shutil,
        "which",
        lambda name, **kwargs: str(host) if name == "node" else "/host/fnm" if fnm else None,
    )
    calls = []

    def execute(argv, cwd, timeout, log, **kwargs):
        calls.append((argv, kwargs["env"]))
        assert cwd != project
        if argv[0] == "/host/fnm":
            assert argv == [
                "/host/fnm",
                "exec",
                "--using=24.12.0",
                "node",
                "-p",
                "process.execPath",
            ]
            return 0, str(pinned) + "\n"
        selected = "24.12.0" if Path(argv[0]) == pinned else version
        return 0, json.dumps({"version": selected, "executable": argv[0]})

    monkeypatch.setattr(node_runtime, "execute", execute)
    return project, pinned, calls


def test_fnm_installed_pin_overrides_inherited_path_and_records_evidence(tmp_path, monkeypatch):
    project, pinned, calls = runtime_fixture(tmp_path, monkeypatch)
    before = os.environ.get("PATH")
    env, evidence = node_environment(project, tmp_path / "preflight")
    assert env["PATH"].split(os.pathsep)[0] == str(pinned.parent)
    assert evidence == {
        "requested_version": "24.12.0",
        "version": "24.12.0",
        "executable": str(pinned),
    }
    assert len(calls) == 3 and os.environ.get("PATH") == before
    assert json.loads(next((tmp_path / "preflight").glob("node-*.json")).read_text()) == evidence


def test_matching_runtime_uses_stable_binary_without_calling_fnm(tmp_path, monkeypatch):
    project, _, calls = runtime_fixture(tmp_path, monkeypatch, version="24.12.0")
    env, _ = node_environment(project, tmp_path / "preflight")
    assert env["PATH"].startswith(str(tmp_path / "host")) and len(calls) == 1


def test_missing_pin_is_a_prerequisite_failure_not_an_automatic_install(tmp_path, monkeypatch):
    project, _, calls = runtime_fixture(tmp_path, monkeypatch, fnm=False)
    with pytest.raises(PipelineError, match=r"Node 24.12.0 is required; host resolved 22.18.0"):
        node_environment(project, tmp_path / "preflight")
    assert len(calls) == 1


def test_runtime_resolution_preserves_cancellation(tmp_path, monkeypatch):
    project, _, _ = runtime_fixture(tmp_path, monkeypatch)

    def cancelled(*args, **kwargs):
        raise StopRequested("pause")

    monkeypatch.setattr(node_runtime, "execute", cancelled)
    with pytest.raises(StopRequested):
        node_environment(project, tmp_path / "preflight")


def test_bootstrap_missing_dependencies_are_detected_during_preparation(service, tmp_path):
    svc, rid, repo = service
    cfg = package(repo)
    cfg.commands = {"bootstrap": bootstrap()}
    (repo / "package-lock.json").unlink()
    with pytest.raises(PipelineError, match="package-lock.json"):
        prepare_dependencies(repo, cfg, tmp_path / "preflight")


def test_runtime_failure_stops_before_model_calls_without_using_fix_budget(service, monkeypatch):
    svc, rid, _ = service

    class MissingRuntime(TestRunner):
        def prepare(self, *args):
            raise PipelineError("Node 24.12.0 is required; host resolved 22.18.0")

    agent = FakeAgent()
    Runner(svc.store, agent, MissingRuntime())._execute(rid)
    run = svc.store.get(rid)
    assert run["state"] == "needs_input" and run["iteration"] == 0
    assert not agent.calls and not svc.store.steps(rid)


def test_python_test_execution_receives_pinned_node_path(service, tmp_path, monkeypatch):
    from dev_orchestra.infrastructure import test_runner

    svc, _, repo = service
    cfg = package(repo)
    cfg.commands = {"bootstrap": bootstrap()}
    runtime = {
        "requested_version": "24.12.0",
        "version": "24.12.0",
        "executable": "/pinned/bin/node",
    }
    monkeypatch.setattr(
        node_runtime, "node_environment", lambda *args: ({"PATH": "/pinned/bin"}, runtime)
    )
    monkeypatch.setattr(test_runner, "sandbox_command", lambda w, o, s, argv, *args, **kwargs: argv)

    def execute(argv, cwd, timeout, log, **kwargs):
        assert argv == cfg.commands["bootstrap"].argv
        assert kwargs["env"]["PATH"] == "/pinned/bin"
        (repo / "build/junit.xml").write_text(
            '<testsuite tests="1"><testcase name="actual"/></testsuite>'
        )
        return 0, "ok"

    monkeypatch.setattr(test_runner, "execute", execute)
    result = TestRunner().run(repo, cfg, tmp_path / "execution")
    assert result["passed"] is True
    assert result["commands"][0]["node_runtime"] == runtime


def test_python_bootstrap_routes_pnpm_preparation(service, tmp_path, monkeypatch):
    from dev_orchestra.infrastructure import dependencies

    _, _, repo = service
    cfg = package(repo, packageManager="pnpm@10.18.3")
    cfg.commands = {"bootstrap": bootstrap()}
    calls = []
    monkeypatch.setattr(dependencies, "prepare_pnpm", lambda *args, **kwargs: calls.append(args))
    prepare_dependencies(repo, cfg, tmp_path / "preflight")
    assert (
        len(calls) == 1 and calls[0][0] == repo and calls[0][2]["packageManager"] == "pnpm@10.18.3"
    )
