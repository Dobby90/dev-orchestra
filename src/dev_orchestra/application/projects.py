"""Project validation; persistence is added with the database expansion phase."""

from pathlib import Path

from dev_orchestra.application.contracts import RepositoryInfo
from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.config import Config
from dev_orchestra.infrastructure.git import Git


class ProjectService:
    def __init__(self, allowed_roots: list[Path]):
        self.allowed_roots = tuple(p.expanduser().resolve(strict=True) for p in allowed_roots)

    def validate_repository(self, path: Path, base_branch: str) -> RepositoryInfo:
        requested = path.expanduser().absolute()
        if any(p.is_symlink() for p in [requested, *requested.parents]):
            raise ApplicationError("UNSAFE_PATH", "symlink repository paths are not allowed")
        canonical = requested.resolve(strict=True)
        if not any(canonical.is_relative_to(p) for p in self.allowed_roots):
            raise ApplicationError("PATH_NOT_ALLOWED", "repository is outside allowed roots")
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
