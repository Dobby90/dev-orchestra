"""Frozen pnpm preparation must preserve source, version pins and offline isolation."""

import json
import os
import shutil
import subprocess
from contextlib import nullcontext

import pytest

from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure import pnpm
from dev_orchestra.infrastructure.pnpm import pnpm_layout, prepare_pnpm
from dev_orchestra.infrastructure.process import environment


def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    package = {"name": "fixture", "packageManager": "pnpm@10.18.3"}
    (root / "package.json").write_text(json.dumps(package))
    (root / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\nimporters:\n  .: {}\n  apps/web: {}\n"
    )
    (root / "pnpm-workspace.yaml").write_text("packages: ['apps/*']\n")
    (root / "apps/web").mkdir(parents=True)
    (root / "apps/web/package.json").write_text('{"name":"web"}')
    directory = tmp_path / "preflight"
    directory.mkdir()
    return root, package, directory


def fake_host(tmp_path, monkeypatch, *, version="10.18.3", failed=False):
    store = tmp_path / "store"
    store.mkdir()
    calls = []
    monkeypatch.setattr(pnpm.shutil, "which", lambda *args, **kwargs: "/host/pnpm")
    monkeypatch.setattr(pnpm, "host_root", lambda: tmp_path / "resources")
    monkeypatch.setattr(pnpm, "slot", lambda *args, **kwargs: nullcontext())

    def sandbox(worktree, outputs, scratch, argv):
        calls.append((argv, outputs))
        if "install" in argv:
            assert outputs == [worktree / "node_modules", worktree / "apps/web/node_modules", store]
            for flag in ["--offline", "--frozen-lockfile", "--ignore-scripts", "--ignore-pnpmfile"]:
                assert flag in argv
        else:
            assert not outputs
        return argv

    def execute(argv, cwd, timeout, log, **kwargs):
        env = kwargs["env"]
        assert env["COREPACK_ENABLE_NETWORK"] == "0" and env["NPM_CONFIG_USERCONFIG"] == "/dev/null"
        assert env["PATH"].split(os.pathsep)[0] == "/pinned/node/bin"
        if "--version" in argv:
            return 0, version + "\n"
        if "store" in argv:
            return 0, str(store) + "\n"
        return (1, "missing cached dependency") if failed else (0, "ready")

    monkeypatch.setattr(pnpm, "sandbox_command", sandbox)
    monkeypatch.setattr(pnpm, "execute", execute)
    return calls


def test_workspace_install_is_offline_frozen_and_uses_pinned_node(tmp_path, monkeypatch):
    root, package, directory = project(tmp_path)
    calls = fake_host(tmp_path, monkeypatch)
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    prepare_pnpm(
        root,
        root,
        package,
        directory,
        ["package.json"],
        lambda: None,
        node_env={"PATH": "/pinned/node/bin"},
    )
    assert len(calls) == 2
    assert {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    marker = json.loads(next(directory.glob("pnpm-*.json")).read_text())
    assert marker["ready"] is True and marker["version"] == "10.18.3"


def test_cached_install_failure_keeps_marker_unready(tmp_path, monkeypatch):
    root, package, directory = project(tmp_path)
    fake_host(tmp_path, monkeypatch, failed=True)
    with pytest.raises(PipelineError, match="missing cached dependency"):
        prepare_pnpm(
            root, root, package, directory, [], lambda: None, node_env={"PATH": "/pinned/node/bin"}
        )
    assert json.loads(next(directory.glob("pnpm-*.json")).read_text())["ready"] is False


def test_wrong_pnpm_version_stops_before_reading_store_or_installing(tmp_path, monkeypatch):
    root, package, directory = project(tmp_path)
    calls = fake_host(tmp_path, monkeypatch, version="11.25.0")
    with pytest.raises(PipelineError, match="pnpm 10.18.3 is required"):
        prepare_pnpm(
            root, root, package, directory, [], lambda: None, node_env={"PATH": "/pinned/node/bin"}
        )
    assert len(calls) == 1


@pytest.mark.parametrize("name", ["../outside", "/absolute", "apps/link"])
def test_importer_cannot_escape_workspace_or_follow_links(tmp_path, name):
    root, package, _ = project(tmp_path)
    if name == "apps/link":
        (root / "apps/link").symlink_to(root / "apps/web", target_is_directory=True)
    (root / "pnpm-lock.yaml").write_text("importers:\n  .: {}\n  " + name + ": {}\n")
    with pytest.raises(PipelineError):
        pnpm_layout(root, root, package)


def test_missing_lock_and_yaml_aliases_are_prerequisite_failures(tmp_path):
    root, package, _ = project(tmp_path)
    (root / "pnpm-lock.yaml").unlink()
    with pytest.raises(PipelineError, match="pnpm-lock.yaml"):
        pnpm_layout(root, root, package)
    (root / "pnpm-lock.yaml").write_text("importers: &a {'.': {}}\npackages: *a\n")
    with pytest.raises(PipelineError, match="invalid pnpm workspace lockfile"):
        pnpm_layout(root, root, package)


def test_tracked_dependency_source_is_never_writable(tmp_path, monkeypatch):
    root, package, directory = project(tmp_path)
    calls = fake_host(tmp_path, monkeypatch)
    with pytest.raises(PipelineError, match="tracked source"):
        prepare_pnpm(
            root, root, package, directory, ["apps/web/node_modules/source.ts"], lambda: None
        )
    assert not calls


def test_pnpm_preparation_preserves_pause(tmp_path, monkeypatch):
    root, package, directory = project(tmp_path)
    fake_host(tmp_path, monkeypatch)

    def paused(*args, **kwargs):
        raise StopRequested("pause")

    monkeypatch.setattr(pnpm, "execute", paused)
    with pytest.raises(StopRequested):
        prepare_pnpm(root, root, package, directory, [], lambda: None)


@pytest.mark.skipif(
    not shutil.which("pnpm") or os.getenv("DEVORCHESTRA_TEST_SANDBOX") != "1",
    reason="opt-in offline pnpm sandbox installation",
)
def test_real_offline_workspace_install_never_runs_hooks_or_lifecycle_scripts(
    tmp_path, monkeypatch
):
    root, _, directory = project(tmp_path)
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    monkeypatch.setenv("HOME", str(host_home))
    env = environment() | {"COREPACK_ENABLE_NETWORK": "0", "COREPACK_ENABLE_PROJECT_SPEC": "0"}

    def command(args):
        result = subprocess.run(
            [shutil.which("pnpm"), *args],
            cwd=root if args[0] == "install" else tmp_path,
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    version = command(["--version"]).splitlines()[-1]
    package = {
        "name": "fixture",
        "packageManager": "pnpm@" + version,
        "dependencies": {"web": "workspace:*"},
        "scripts": {
            "postinstall": "node -e \"require('fs').writeFileSync('node_modules/UNSAFE','bad')\""
        },
    }
    (root / "package.json").write_text(json.dumps(package))
    (root / ".pnpmfile.cjs").write_text("throw new Error('repository hook executed');")
    (root / "pnpm-lock.yaml").unlink()
    command(["install", "--lockfile-only", "--offline", "--ignore-scripts", "--ignore-pnpmfile"])
    from pathlib import Path

    store = Path(command(["store", "path"]))
    store.mkdir(parents=True, exist_ok=True)
    before = (root / "pnpm-lock.yaml").read_bytes()
    prepare_pnpm(root, root, package, directory, ["package.json", ".pnpmfile.cjs"], lambda: None)
    assert (root / "node_modules/web/package.json").is_file()
    assert not (root / "node_modules/UNSAFE").exists()
    assert (root / "pnpm-lock.yaml").read_bytes() == before
