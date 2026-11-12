"""Project validation; persistence is added with the database expansion phase."""

import os
from pathlib import Path

from dev_orchestra.application.contracts import RepositoryInfo
from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.config import Config
from dev_orchestra.infrastructure.git import Git


class ProjectService:
    def __init__(self, allowed_roots=None):
        # Retained for callers from older versions; registration authorizes each repository.
        pass

    @staticmethod
    def directory(path: Path) -> Path:
        requested = path.expanduser().absolute()
        if any(p.is_symlink() for p in [requested, *requested.parents]):
            raise ApplicationError("UNSAFE_PATH", "symlink paths are not allowed")
        canonical = requested.resolve(strict=True)
        if not canonical.is_dir():
            raise ApplicationError("VALIDATION_ERROR", "directory required")
        return canonical

    def directories(self, path: str | None):
        root = self.directory(Path(path) if path else Path.home())
        children = []
        truncated = False
        with os.scandir(root) as entries:
            for entry in entries:
                if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
                    continue
                if len(children) == 200:
                    truncated = True
                    break
                children.append({"name": entry.name, "path": str(root / entry.name)})
        children.sort(key=lambda child: child["name"].casefold())
        return {
            "path": str(root),
            "parent": str(root.parent) if root.parent != root else None,
            "items": children,
            "truncated": truncated,
        }

    def validate_repository(self, path: Path, base_branch: str) -> RepositoryInfo:
        canonical = self.directory(path)
        git = Git(canonical)
        top = Path(git.call("rev-parse", "--show-toplevel")).resolve(strict=True)
        if top != canonical:
            raise ApplicationError("REPOSITORY_ROOT_REQUIRED", "register the repository root")
        return RepositoryInfo(
            repository_path=canonical,
            git_common_directory=git.common(),
            base_branch=base_branch,
            base_sha=git.baseline(base_branch),
        )

    def inspect_repository(self, path: Path):
        canonical = self.directory(path)
        git = Git(canonical)
        top = Path(git.call("rev-parse", "--show-toplevel")).resolve(strict=True)
        if top != canonical:
            raise ApplicationError("REPOSITORY_ROOT_REQUIRED", "register the repository root")
        branches = git.call("for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()
        if not branches:
            raise ApplicationError("VALIDATION_ERROR", "a committed local branch is required")
        current = git.call("rev-parse", "--abbrev-ref", "HEAD")
        return {
            "repository_path": str(canonical),
            "branches": branches,
            "default_branch": current if current in branches else branches[0],
        }

    def detect_validation(self, path: Path, base_branch: str):
        from dev_orchestra.application.validation import detect_validation

        info = self.validate_repository(path, base_branch)
        return detect_validation(info)

    def validate_policy(self, config: Config) -> Config:
        snapshot = Config.model_validate(config.model_dump())
        if not snapshot.tests.commands:
            raise ApplicationError("TESTS_REQUIRED", "configure at least one test command")
        if not {"critical", "high"} <= set(snapshot.review.blocking_severities):
            raise ApplicationError(
                "BLOCKING_POLICY_REQUIRED", "critical and high must block approval"
            )
        return snapshot
