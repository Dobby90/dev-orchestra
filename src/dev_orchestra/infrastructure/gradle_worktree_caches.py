"""Record the host-managed Gradle cache trees created in a single Run's Worktree."""

import json
from contextlib import contextmanager
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained, write
from dev_orchestra.infrastructure.gradle_preparation import preparation_control
from dev_orchestra.infrastructure.java_cache import cache_identity


def gradle_cache_roots(worktree, commands, output_paths):
    from dev_orchestra.infrastructure.java import cache_paths, java_tool

    gradle = [command for command in commands if java_tool(command) == "gradle"]
    if not gradle:
        return set()
    outputs = [contained(worktree, name) for name in output_paths]
    roots = set()
    managed = (".devorchestra-cache/gradle", ".devorchestra-cache/project")
    for command in gradle:
        try:
            paths = cache_paths(command)
        except PipelineError:
            continue  # Validation still diagnoses invalid Gradle arguments in its normal path.
        for name, expected in zip(paths, managed, strict=True):
            if name == expected:
                path = contained(worktree, (Path(command.working_directory) / name).as_posix())
                if any(path.is_relative_to(output) for output in outputs):
                    roots.add(path)
    return roots


class GradleWorktreeCaches:
    def __init__(self, preflight, worktree):
        self.worktree = worktree.resolve()
        directory = Path(preflight).resolve()
        self.manifest = (
            None
            if directory.is_relative_to(self.worktree)
            else contained(directory, "gradle-worktree-caches.json")
        )

    def identity(self):
        info = self.worktree.stat()
        return [str(self.worktree), info.st_dev, info.st_ino]

    def records(self):
        if self.manifest is None or not self.manifest.is_file():
            return {}
        try:
            data = json.loads(self.manifest.read_text())
            if data["worktree"] == self.identity() and isinstance(data["caches"], dict):
                return data["caches"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return {}

    @contextmanager
    def record(self, paths, stop):
        """Track new cache roots and previously verified roots across successful writers."""
        if not paths or self.manifest is None:
            yield
            return
        records = self.records()
        eligible = []
        poll = preparation_control(stop)
        for path in sorted(paths):
            name = path.relative_to(self.worktree).as_posix()
            prior = records.get(name)
            if not path.exists() or (
                prior and prior == cache_identity([path], poll, exclusive=True)
            ):
                eligible.append((name, path))

        yield

        for name, path in eligible:
            if identity := cache_identity([path], poll, exclusive=True):
                records[name] = identity
            else:
                records.pop(name, None)
        if eligible:
            try:
                write(self.manifest, {"worktree": self.identity(), "caches": records})
            except OSError:
                pass  # Cleanup will preserve any cache without matching ownership evidence.

    def verified(self, paths):
        records = self.records()
        verified = {}
        for path in sorted(paths):
            name = path.relative_to(self.worktree).as_posix()
            if not path.exists() or (path.is_dir() and not any(path.iterdir())):
                continue
            expected = records.get(name)
            if not expected or cache_identity([path], lambda: None, exclusive=True) != expected:
                raise PipelineError(f"확인되지 않은 Gradle 캐시를 보존합니다: {name}")
            verified[path] = expected
        return verified
