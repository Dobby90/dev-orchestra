"""Gradle cleanup accepts only cache trees recorded by the host for this Worktree."""

import os
import subprocess

import pytest

from dev_orchestra.application.worktree_cleanup import preserve_test_reports
from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as Commands
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure import java, test_runner
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.gradle_worktree_caches import (
    GradleWorktreeCaches,
    gradle_cache_roots,
)


@pytest.fixture
def gradle_run(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    git = Git(worktree)
    git.call("config", "user.name", "Test")
    git.call("config", "user.email", "test@example.invalid")
    (worktree / ".gitignore").write_text(".devorchestra-cache/project/\n")
    (worktree / "main.java").write_text("class Main {}\n")
    git.commit("initial")
    directory = tmp_path / "run"
    argv = [
        "gradle",
        "--offline",
        "--gradle-user-home",
        ".devorchestra-cache/gradle",
        "--project-cache-dir=.devorchestra-cache/project",
    ]
    config = Commands(
        commands={
            "build": Command(kind="check", argv=[*argv, "assemble"]),
            "unit": Command(argv=[*argv, "test"], junit_report="build/test.xml"),
        },
        output_paths=["build", ".devorchestra-cache"],
    )
    home, project = sorted(
        gradle_cache_roots(worktree, config.commands.values(), config.output_paths)
    )
    run = {
        "directory": str(directory),
        "worktree": str(worktree),
        "current_sha": git.sha(),
        "config": {"tests": config.model_dump()},
    }

    def prepare(*args):
        for root in (home, project):
            root.mkdir(parents=True, exist_ok=True)
            (root / "initial.bin").write_bytes(b"seed")

    monkeypatch.setattr(java, "_prepare_java", prepare)
    monkeypatch.setattr("dev_orchestra.application.worktree_cleanup.checkpoints", lambda *_: {})
    return run, worktree, directory, config, home, project


def test_cleanup_archives_confirmed_gradle_caches_after_preparation_and_execution(
    gradle_run, monkeypatch
):
    run, worktree, directory, config, home, project = gradle_run
    preflight = directory / "preflight"
    java.prepare_java(worktree, config, preflight)

    def execute(argv, cwd, *args, **kwargs):
        frozen = config.commands["build"].argv
        assert argv[: len(frozen)] == frozen
        (home / "initial.bin").write_bytes(b"updated seed")
        (project / "generated.bin").write_bytes(b"new cache")
        return 0, ""

    monkeypatch.setattr(test_runner, "execute", execute)
    monkeypatch.setattr(test_runner, "sandbox_command", lambda root, paths, temp, argv, *args: argv)
    monkeypatch.setattr(java, "java_environment", lambda *_: {})
    monkeypatch.setattr("dev_orchestra.infrastructure.git.execution_directory", lambda: None)
    build = config.model_copy(update={"commands": {"build": config.commands["build"]}})
    assert test_runner.TestRunner(preflight=preflight).run(worktree, build, directory / "test")[
        "passed"
    ]

    preserve_test_reports(None, run)

    assert not home.exists() and not project.exists()
    (archive,) = directory.glob("cleanup-gradle-caches-*")
    assert (archive / home.relative_to(worktree) / "initial.bin").read_bytes() == b"updated seed"
    assert (archive / project.relative_to(worktree) / "generated.bin").read_bytes() == b"new cache"


@pytest.mark.parametrize(
    "change",
    [
        "preexisting",
        "modified",
        "ignored",
        "symlink",
        "hardlink",
        "outside",
        "other_worktree",
        "legacy",
    ],
)
def test_cleanup_keeps_unverified_caches_and_unrelated_files(gradle_run, change):
    run, worktree, directory, config, home, project = gradle_run
    if change == "preexisting":
        home.mkdir(parents=True)
        (home / "user.txt").write_text("preserve")
    preflight = directory / "preflight"
    java.prepare_java(worktree, config, preflight)
    receipt = GradleWorktreeCaches(preflight, worktree).manifest
    if change == "modified":
        (home / "initial.bin").write_bytes(b"different")
    elif change == "ignored":
        (project / "user.txt").write_text("preserve")
    elif change == "symlink":
        (home / "link").symlink_to(worktree / "main.java")
    elif change == "hardlink":
        os.link(worktree / "main.java", home / "link")
    elif change == "outside":
        (worktree / "other.txt").write_text("preserve")
    elif change == "other_worktree":
        receipt.write_text(
            receipt.read_text().replace(str(worktree), str(worktree.parent / "other"))
        )
    elif change == "legacy":
        receipt.unlink()

    with pytest.raises(PipelineError, match="확인되지 않은"):
        preserve_test_reports(None, run)
    assert home.is_dir() and project.is_dir()
    assert not list(directory.glob("cleanup-gradle-caches-*"))


def test_interrupted_gradle_writer_does_not_bless_changed_cache(gradle_run):
    run, worktree, directory, config, home, _ = gradle_run
    preflight = directory / "preflight"
    java.prepare_java(worktree, config, preflight)
    tracker = GradleWorktreeCaches(preflight, worktree)
    with pytest.raises(RuntimeError), tracker.record({home}, lambda: None):
        (home / "initial.bin").write_bytes(b"changed during interruption")
        raise RuntimeError("interrupted")
    with pytest.raises(PipelineError, match="확인되지 않은"):
        preserve_test_reports(None, run)
    assert (home / "initial.bin").read_bytes() == b"changed during interruption"
