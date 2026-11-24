"""Identify and fingerprint optional Jest files for later Worktree cleanup."""

import os
import re
import stat
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained, digest

MAX_FILE_BYTES = 32 * 2**20
MAX_CACHE_BYTES = 256 * 2**20
MAX_CACHE_FILES = 5000
CACHE_FILE = re.compile(
    r"(?:haste-map|perf-cache)-[0-9a-f]{32}(?:-[0-9a-f]{32}){1,2}"
    r"|jest-transform-cache-[0-9a-f]{32}-[0-9a-f]{32}/[0-9a-f]{2}/"
    r"[\w.-]+_[0-9a-f]{32}(?:\.map)?"
)


def cache_path(worktree, command, outputs):
    values = []
    for index, arg in enumerate(command.argv):
        if arg == "--cacheDirectory":
            values.extend(command.argv[index + 1 : index + 2] or [""])
        elif arg.startswith("--cacheDirectory="):
            values.append(arg.partition("=")[2])
    if len(values) != 1 or not values[0] or Path(values[0]).is_absolute():
        return None
    try:
        cache = contained(worktree, str(Path(command.working_directory) / values[0]))
    except PipelineError:
        return None
    return cache if any(cache.is_relative_to(output) for output in outputs) else None


def is_cache_file(path, cache):
    return (
        cache is not None
        and path.is_relative_to(cache)
        and bool(CACHE_FILE.fullmatch(path.relative_to(cache).as_posix()))
    )


def cache_files(git, cache):
    if cache is None:
        return set()
    return set(
        filter(
            None,
            git.call(
                "--literal-pathspecs",
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                cache.relative_to(git.repo).as_posix(),
            ).split("\0"),
        )
    )


def output_digest(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > MAX_FILE_BYTES
        ):
            raise PipelineError("unsupported Jest output file")
        content = stream.read(MAX_FILE_BYTES + 1)
        after = path.lstat()
        if len(content) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_ctime_ns,
        ):
            raise PipelineError("Jest output changed while reading")
    return digest(content)


def generated_outputs(git, report, cache, before):
    """Only the converted report and new, recognized cache files are eligible."""
    relative_report = report.relative_to(git.repo).as_posix()
    result = {relative_report: output_digest(contained(git.repo, relative_report))}
    if before is None:
        return result
    names = cache_files(git, cache) - before
    if len(names) > MAX_CACHE_FILES:
        return result
    remaining = MAX_CACHE_BYTES
    for name in sorted(names):
        if not is_cache_file(git.repo / name, cache):
            continue
        try:
            path = contained(git.repo, name)
            remaining -= path.lstat().st_size
            if remaining < 0:
                break
            result[name] = output_digest(path)
        except (OSError, PipelineError):
            continue
    return result
