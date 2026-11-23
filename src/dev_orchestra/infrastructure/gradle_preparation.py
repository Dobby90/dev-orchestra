"""Host-owned Gradle readiness records. Test and approval evidence lives separately."""

import json
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.java_cache import cache_identity, file_identity, fingerprint

_progress = ContextVar("gradle_preparation_observer", default=None)


@contextmanager
def observe_preparation(observer):
    token = _progress.set(observer)
    try:
        yield
    finally:
        _progress.reset(token)


class ModuleProgress:
    def __init__(self, directories):
        self.remaining = Counter(directories)
        self.total = len(self.remaining)
        self.rechecked = set()
        self.checked = self.reused = 0
        if self.total:
            self.publish()

    def publish(self):
        if observer := _progress.get():
            observer(dict(total=self.total, checked=self.checked, reused=self.reused, active=True))

    def complete(self, directory, checked=False):
        if checked:
            self.rechecked.add(directory)
        self.remaining[directory] -= 1
        if self.remaining[directory] == 0:
            if directory in self.rechecked:
                self.checked += 1
            else:
                self.reused += 1
            self.publish()


def tool_identity(path):
    path = Path(path).resolve()
    try:
        return str(path), file_identity(path.stat())
    except FileNotFoundError:
        return str(path), None


def readiness_binding(worktree, cwd, executable, sha, policy, jdk, environment, required):
    inputs = [executable]
    if executable.name == "gradlew":
        wrapper = executable.parent / "gradle/wrapper"
        inputs.extend((wrapper / "gradle-wrapper.jar", wrapper / "gradle-wrapper.properties"))
    for parent in (cwd, *cwd.parents):
        if parent.is_relative_to(worktree):
            inputs.extend(
                parent / name
                for name in (
                    "build.gradle",
                    "build.gradle.kts",
                    "settings.gradle",
                    "settings.gradle.kts",
                    "gradle.properties",
                    "gradle/libs.versions.toml",
                )
            )
    return [
        1,
        sha,
        policy,
        worktree.stat().st_ino,
        jdk,
        environment,
        required,
        [tool_identity(path) for path in inputs],
    ]


class ReadinessRecord:
    def __init__(self, directory, environment, binding):
        self.path = directory / "gradle-readiness" / (fingerprint(environment) + ".json")
        self.binding = fingerprint(binding) if binding is not None else None

    def matches(self, outputs, stop):
        if self.binding is not None and self.path.is_file():
            try:
                data = json.loads(self.path.read_text())
            except (OSError, ValueError):
                data = None
            if isinstance(data, dict) and data.get("binding") == self.binding:
                previous = data.get("cache")
                if previous and previous == cache_identity(outputs, stop):
                    return True
        self.path.unlink(missing_ok=True)
        return False

    def save(self, outputs, stop):
        if self.binding is not None and (identity := cache_identity(outputs, stop)):
            write(self.path, {"binding": self.binding, "cache": identity})
