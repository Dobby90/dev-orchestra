"""Run-owned Gradle seeds and independent, offline Java cache copies."""

import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import uuid
from pathlib import Path

from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure.files import write


def check_stop(stop):
    if value := stop():
        raise StopRequested(value)


def file_identity(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def cache_identity(roots, stop, *, toolchain=False):
    """Include every entry and kernel change time, including replaced or backdated files."""
    digest = hashlib.sha256()
    try:
        for root in roots:
            pending = [str(root)]
            while pending:
                check_stop(stop)
                parent = pending.pop()
                directory = os.lstat(parent)
                if not stat.S_ISDIR(directory.st_mode):
                    return None
                digest.update(os.fsencode(parent) + b"\0" + repr(file_identity(directory)).encode())
                with os.scandir(parent) as entries:
                    for entry in sorted(entries, key=lambda e: e.name):
                        check_stop(stop)
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISDIR(info.st_mode):
                            pending.append(entry.path)
                        elif toolchain and stat.S_ISLNK(info.st_mode):
                            try:
                                resolved = Path(entry.path).resolve()
                            except RuntimeError:  # Symlink loop on Python 3.11/3.12.
                                return None
                            digest.update(os.fsencode(entry.path) + b"\0" + os.fsencode(resolved))
                            try:
                                target = resolved.stat()
                            except FileNotFoundError:
                                continue
                            if stat.S_ISDIR(target.st_mode) and not resolved.is_relative_to(
                                Path(root).resolve()
                            ):
                                return None
                            digest.update(repr(file_identity(target)).encode())
                        elif stat.S_ISREG(info.st_mode):
                            digest.update(
                                os.fsencode(entry.name) + b"\0" + repr(file_identity(info)).encode()
                            )
                        else:
                            return None
    except OSError:
        return None
    return digest.hexdigest()


def cache_files(source, stop):
    """Select only distributable artifacts. Never import host settings or links."""
    if not source.is_dir() or source.is_symlink():
        return
    for parent, dirs, files in os.walk(source, followlinks=False):
        check_stop(stop)
        dirs[:] = sorted(
            n for n in dirs if not n.startswith(".") and not (Path(parent) / n).is_symlink()
        )
        for name in sorted(files):
            check_stop(stop)
            path = Path(parent) / name
            if (
                name.startswith(".env")
                or name
                in {"settings.xml", "settings-security.xml", "gradle.properties", "gc.properties"}
                or path.suffix in {".lock", ".lck", ".key", ".pem", ".part", ".lastUpdated"}
            ):
                continue
            info = path.lstat()
            if stat.S_ISREG(info.st_mode):
                yield path.relative_to(source), info


# clonefile creates independent copy-on-write inodes on macOS volumes that support it.
_clonefile = (
    getattr(ctypes.CDLL(None, use_errno=True), "clonefile", None)
    if sys.platform == "darwin"
    else None
)


def copy_artifact(source, target, mode, stop, expected, *, clone=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(".seed-" + uuid.uuid4().hex)
    try:
        cloned = False
        if clone and _clonefile is not None:
            cloned = _clonefile(os.fsencode(source), os.fsencode(temp), 0) == 0
            if not cloned and (error := ctypes.get_errno()) not in {
                errno.ENOTSUP,
                errno.EXDEV,
                errno.ENOSYS,
                errno.EINVAL,
            }:
                raise OSError(error, os.strerror(error), str(source))
        if not cloned:
            fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as src, temp.open("xb") as dst:
                if file_identity(os.fstat(fd)) != expected:
                    raise PipelineError("host Java cache changed before copy")
                while chunk := src.read(2**20):
                    check_stop(stop)
                    dst.write(chunk)
        if file_identity(source.lstat()) != expected:
            raise PipelineError("host Java cache changed during copy; retry preparation")
        temp.chmod(mode)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)


def validate_destination(path):
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise PipelineError("symlink Java cache paths are not allowed")


def seed_cache(source, target, stop):
    if not source.is_dir() or source.is_symlink():
        return
    if source.resolve() == target.resolve():
        return
    if target.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(
        target.resolve()
    ):
        raise PipelineError("source and isolated Java caches must not overlap")
    count = size = 0
    for relative, before in cache_files(source, stop):
        dst = target / relative
        validate_destination(dst)
        if dst.exists():
            continue
        count += 1
        size += before.st_size
        if count > 100000 or size > 4 * 2**30:
            raise PipelineError("Java cache seeding exceeds safety limit")
        copy_artifact(source / relative, dst, before.st_mode & 0o755, stop, file_identity(before))


class GradleSeeds:
    def __init__(self, directory):
        self.directory = directory
        self.groups = {}
        self.included_builds = {}

    def build_root(self, worktree, cwd):
        for root in (cwd, *cwd.parents):
            if not root.is_relative_to(worktree):
                break
            if root.name == "buildSrc":
                return root
            for name in ("settings.gradle", "settings.gradle.kts"):
                settings = root / name
                if not settings.is_file():
                    continue
                if settings not in self.included_builds:
                    self.included_builds[settings] = included_roots(settings)
                included = self.included_builds[settings]
                if included is None:
                    return cwd
                return next((p for p in (cwd, *cwd.parents) if p in included), root)
        return cwd

    def populate(self, worktree, cwd, executable, jdk, distribution, host, target, stop):
        root = self.build_root(worktree, cwd)
        if target.resolve() == host.resolve():
            return
        parts = [Path("caches/modules-2")]
        if distribution:
            parts.append(Path("wrapper/dists") / distribution)
        for part in parts:
            source, destination = (host / part).resolve(), (target / part).resolve()
            if destination.is_relative_to(source) or source.is_relative_to(destination):
                raise PipelineError("source and isolated Java caches must not overlap")
        group = (root, executable, jdk, distribution, host)
        if group not in self.groups:
            self.groups[group] = self.shared_seed(group, host, parts, stop)
        if self.groups[group] is None:
            # An already populated module may only need a small subset of an oversized host cache.
            for part in parts:
                seed_cache(host / part, target / part, stop)
            return
        seed, entries = self.groups[group]
        checked_parents = set()
        for name, identity in entries.items():
            check_stop(stop)
            dst = target / name
            if dst.parent not in checked_parents:
                validate_destination(dst.parent)
                checked_parents.add(dst.parent)
            if dst.is_symlink():
                raise PipelineError("symlink Java cache paths are not allowed")
            src = seed / name
            if not dst.exists():
                copy_artifact(
                    src, dst, identity[2] & 0o755, stop, file_identity(src.stat()), clone=True
                )

    def shared_seed(self, group, host, parts, stop):
        entries = {}
        for part in parts:
            size = 0
            for count, (relative, info) in enumerate(cache_files(host / part, stop), 1):
                size += info.st_size
                if count > 100000 or size > 4 * 2**30:
                    return None
                entries[str(part / relative)] = file_identity(info)
        seed = self.directory / "gradle-seeds" / fingerprint([group, entries])
        seal = seed.with_suffix(".stamp")
        if seed.is_dir() and seal.is_file():
            stamp = cache_identity([seed], stop)
            if stamp and seal.read_bytes() == stamp.encode():
                return seed, entries
        seed.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=seed.parent) as staging:
            pending = Path(staging) / "seed"
            pending.mkdir()
            for name, identity in entries.items():
                check_stop(stop)
                copy_artifact(host / name, pending / name, identity[2] & 0o555, stop, identity)
            if seed.exists() or seed.is_symlink():
                seed.rename(Path(staging) / "previous")
            pending.rename(seed)
            write(seal, cache_identity([seed], stop))
        return seed, entries


def included_roots(settings):
    """Composite builds may use a single build file without their own settings file."""
    if settings.is_symlink() or settings.stat().st_size > 2**20:
        return None
    text = settings.read_text(errors="replace")
    roots = set()
    for call in re.finditer(r"\bincludeBuild\b", text):
        literal = re.match(
            r"""\s*\(?\s*(["'])([^"'$\\\r\n]+)\1\s*(?=\)|\{|\n|;|$)""", text[call.end() :]
        )
        if not literal:
            return None
        roots.add((settings.parent / literal[2]).resolve())
    return roots
