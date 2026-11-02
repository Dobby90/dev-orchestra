"""Only host code mutates Git metadata; never changes the original checkout."""

import subprocess
from pathlib import Path

from dev_orchestra.domain.models import PipelineError


class Git:
    def __init__(self, repo: Path, ignore=()):
        self.repo = repo.resolve()
        self.exclusions = [f":(exclude){p}" for p in ignore]

    def call(self, *args, input=None):
        # Git clean/smudge/process drivers execute outside agent sandboxes.
        config = subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "config",
                "--get-regexp",
                r"^filter\..*\.(clean|smudge|process|driver)$",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if config.returncode == 0 and config.stdout.strip():
            raise PipelineError(
                "repository executable filters require an explicit isolation policy"
            )
        if config.returncode not in (0, 1):
            raise PipelineError("cannot inspect repository filter policy")
        proc = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(self.repo),
                *args,
            ],
            input=input,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode:
            raise PipelineError(f"git {args[0]} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def sha(self):
        return self.call("rev-parse", "HEAD")

    def common(self):
        return Path(self.call("rev-parse", "--path-format=absolute", "--git-common-dir"))

    def baseline(self, branch):
        self.call("check-ref-format", "--branch", branch)
        return self.call("rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}")

    def create(self, path, branch, sha):
        self.call("check-ref-format", "--branch", branch)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.call("worktree", "add", "-b", branch, str(path), sha)

    def dirty(self):
        return self.call(
            "status", "--porcelain", "--untracked-files=all", "--", ".", *self.exclusions
        )

    def commit(self, message):
        self.call("add", "--all", "--", ".", *self.exclusions)
        if self.call("diff", "--cached", "--name-only"):
            self.call("commit", "-m", message)
        return self.sha()

    def diff(self, base, sha):
        return self.call("diff", "--no-ext-diff", "--no-textconv", f"{base}...{sha}", "--")

    def remove(self, path):
        self.call("worktree", "remove", str(path))
