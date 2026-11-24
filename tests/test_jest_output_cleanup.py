"""Only Jest outputs tied to the host's test evidence leave the Worktree."""

import json
import subprocess

import pytest

from dev_orchestra.application import worktree_cleanup
from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as Commands
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure import test_runner
from dev_orchestra.infrastructure.git import Git

CACHE = "build/jest-cache/"
CACHED_FILE = CACHE + f"perf-cache-{'a' * 32}-{'b' * 32}"
PREEXISTING_FILE = CACHE + f"haste-map-{'c' * 32}-{'d' * 32}-{'e' * 32}"
JSON_REPORT = "build/test-jest.json"
JUNIT_REPORT = "build/test.xml"


@pytest.fixture
def executed_jest(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    git = Git(worktree)
    git.call("config", "user.name", "Test")
    git.call("config", "user.email", "test@example.invalid")
    (worktree / "main.js").write_text("export default 1\n")
    git.commit("initial")
    directory = tmp_path / "run"
    command = Command(
        argv=[
            "yarn",
            "run",
            "test",
            "--ci",
            "--json",
            f"--outputFile={JSON_REPORT}",
            "--cacheDirectory=build/jest-cache",
        ],
        junit_report=JUNIT_REPORT,
    )
    config = Commands(commands={"unit": command})
    sha = git.sha()

    def run_test(preexisting=False):
        if preexisting:
            (worktree / CACHE).mkdir(parents=True)
            (worktree / PREEXISTING_FILE).write_text("preexisting")

        def execute(argv, cwd, *args, **kwargs):
            assert argv == command.argv
            (cwd / CACHE).mkdir(exist_ok=True)
            (cwd / CACHED_FILE).write_text("new cache")
            (cwd / JSON_REPORT).write_text(
                json.dumps(
                    {
                        "numPassedTests": 1,
                        "numFailedTests": 0,
                        "numPendingTests": 0,
                        "numTotalTests": 1,
                        "testResults": [
                            {
                                "name": "app.spec.js",
                                "assertionResults": [
                                    {"fullName": "existing behavior", "status": "passed"}
                                ],
                            }
                        ],
                    }
                )
            )
            return 0, ""

        monkeypatch.setattr(test_runner, "execute", execute)
        monkeypatch.setattr("dev_orchestra.infrastructure.git.execution_directory", lambda: None)
        monkeypatch.setattr(
            test_runner, "sandbox_command", lambda root, paths, temp, argv, *args: argv
        )
        result = test_runner.TestRunner().run(worktree, config, directory / "attempt")
        assert result["passed"], result
        assert result["commands"][0]["jest_outputs"].keys() == {CACHED_FILE, JSON_REPORT}
        monkeypatch.setattr(
            worktree_cleanup,
            "checkpoints",
            lambda *_: {"tester": {"sha": sha, "revision": 0, "execution": result}},
        )
        return (
            {
                "id": "test-run",
                "directory": str(directory),
                "worktree": str(worktree),
                "current_sha": sha,
                "revision": 0,
                "config": {"tests": config.model_dump()},
            },
            worktree,
            directory,
        )

    return run_test


def test_cleanup_archives_the_verified_jest_json_cache_and_junit(executed_jest):
    run, worktree, directory = executed_jest()
    names = (CACHED_FILE, JSON_REPORT, JUNIT_REPORT)
    originals = {name: (worktree / name).read_bytes() for name in names}

    worktree_cleanup.preserve_test_reports(None, run)

    assert not any((worktree / name).exists() for name in names)
    (archive,) = directory.glob("cleanup-reports-*")
    assert {name: (archive / name).read_bytes() for name in names} == originals


@pytest.mark.parametrize(
    "change", ["unknown", "preexisting", "cache", "symlink", "json", "revision"]
)
def test_cleanup_preserves_unverified_files_and_leaves_reports_in_place(executed_jest, change):
    run, worktree, directory = executed_jest(preexisting=change == "preexisting")
    if change == "unknown":
        (worktree / CACHE / "personal.txt").write_text("preserve")
    elif change in {"cache", "json"}:
        (worktree / (CACHED_FILE if change == "cache" else JSON_REPORT)).write_text("changed")
    elif change == "symlink":
        (worktree / CACHED_FILE).unlink()
        (worktree / CACHED_FILE).symlink_to(worktree / "main.js")
    elif change == "revision":
        run["revision"] = 1

    with pytest.raises(PipelineError):
        worktree_cleanup.preserve_test_reports(None, run)
    assert not list(directory.glob("cleanup-reports-*"))
    assert all((worktree / name).is_file() for name in (CACHED_FILE, JSON_REPORT, JUNIT_REPORT))
