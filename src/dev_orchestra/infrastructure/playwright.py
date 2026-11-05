"""Prepare browsers with a verified official installer, never repository JavaScript."""

import base64
import hashlib
import io
import json
import platform
import re
import shutil
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure.coordination import host_root, slot
from dev_orchestra.infrastructure.files import contained
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process import (
    environment,
    execute,
    execution_directory,
    execution_scope,
    sanitize,
)


def browser_cache():
    return Path.home() / (
        "Library/Caches/ms-playwright" if platform.system() == "Darwin" else ".cache/ms-playwright"
    )


def check_stop(stop):
    if action := stop():
        raise StopRequested(action)


def fetch(url, limit, stop):
    """Only the official npm registry supplies executable installer code."""
    check_stop(stop)
    deadline = time.monotonic() + 120
    with urllib.request.urlopen(url, timeout=15) as response:
        if not response.url.startswith("https://registry.npmjs.org/"):
            raise PipelineError("unexpected Playwright installer registry redirect")
        chunks, size = [], 0
        while chunk := response.read(65536):
            check_stop(stop)
            size += len(chunk)
            if size > limit or time.monotonic() > deadline:
                raise PipelineError("Playwright installer download exceeds safety limit")
            chunks.append(chunk)
    return b"".join(chunks)


def installer(version, destination, stop):
    metadata = json.loads(
        fetch(f"https://registry.npmjs.org/playwright-core/{version}", 2**20, stop)
    )
    if (
        not isinstance(metadata, dict)
        or metadata.get("name") != "playwright-core"
        or metadata.get("version") != version
    ):
        raise PipelineError("Playwright installer package identity mismatch")
    dist = metadata.get("dist")
    integrity = dist.get("integrity") if isinstance(dist, dict) else None
    if not isinstance(integrity, str) or not integrity.startswith("sha512-"):
        raise PipelineError("Playwright installer requires SHA-512 registry integrity")
    data = fetch(
        f"https://registry.npmjs.org/playwright-core/-/playwright-core-{version}.tgz",
        64 * 2**20,
        stop,
    )
    actual = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != integrity:
        raise PipelineError("Playwright installer checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        total = 0
        for member in archive:
            check_stop(stop)
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != "package" or ".." in parts or "\\" in member.name:
                raise PipelineError("unsafe Playwright installer archive path")
            if member.isdir():
                continue
            if not member.isfile() or len(parts) < 2:
                raise PipelineError("unsafe Playwright installer archive entry")
            total += member.size
            if total > 256 * 2**20:
                raise PipelineError("Playwright installer archive exceeds safety limit")
            path = destination.joinpath(*parts[1:])
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open("wb") as output:
                shutil.copyfileobj(source, output)
            path.chmod(0o755 if member.mode & 0o111 else 0o644)
    if not (destination / "cli.js").is_file():
        raise PipelineError("Playwright installer CLI missing")


def installed_version(worktree, cwd):
    def resolve(start, name):
        for parent in [start, *start.parents]:
            if not parent.is_relative_to(worktree):
                break
            path = parent / "node_modules" / name / "package.json"
            if path.is_file():
                return contained(worktree, path.relative_to(worktree).as_posix())
        return None

    start = cwd
    for name in ("@playwright/test", "playwright", "playwright-core"):
        path = resolve(start, name)
        if path is None:
            if name == "playwright-core":
                raise PipelineError("installed playwright-core package not found")
            continue
        if path.stat().st_size > 2**20:
            raise PipelineError("Playwright manifest exceeds safety limit")
        manifest = json.loads(path.read_text())
        if not isinstance(manifest, dict) or manifest.get("name") != name:
            raise PipelineError("installed Playwright package identity mismatch")
        start = path.parent
    version = manifest.get("version", "")
    if not isinstance(version, str) or not re.fullmatch(r"1\.[0-9]+\.[0-9]+", version):
        raise PipelineError(
            "automatic browser preparation requires a stable Playwright 1.x version"
        )
    return version


def installer_command(node, scratch, cache, package):
    """Network is available only to official code with no project or user-data access."""
    if platform.system() != "Darwin":
        raise PipelineError("automatic Playwright browser preparation currently requires macOS")

    def sub(path):
        return "(subpath " + json.dumps(str(Path(path).resolve())) + ")"

    reads = ["/System", "/usr", "/bin", "/sbin", "/Library", "/private/etc", "/dev"]
    for prefix in (Path("/opt/homebrew"), Path("/usr/local")):
        if node.is_relative_to(prefix):
            reads.append(str(prefix))
    reads += [str(scratch), str(cache)]
    profile = (
        "(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)"
        "(allow network-outbound)(allow file-read-metadata)"
        # macOS dyld inspects the filesystem root while locating its shared cache.
        '(allow file-read* (literal "/") (literal '
        + json.dumps(str(node))
        + ") "
        + " ".join(sub(p) for p in reads)
        + ")"
        "(allow file-write* " + sub(scratch) + " " + sub(cache) + ' (literal "/dev/null"))'
    )
    return [
        "/usr/bin/sandbox-exec",
        "-p",
        profile,
        str(node),
        str(package / "cli.js"),
        "install",
    ]


def prepare_browsers(worktree, config, directory, stop=lambda: None):
    commands = [
        (key, command)
        for key, command in config.commands.items()
        if command.env.get("PLAYWRIGHT_BROWSERS_PATH") == str(browser_cache())
    ]
    if not commands:
        return
    node = shutil.which("node")
    if not node:
        raise PipelineError("Node is required to prepare Playwright browsers")
    if platform.system() != "Darwin":
        raise PipelineError("automatic Playwright browser preparation currently requires macOS")
    cache = browser_cache()
    if any(p.is_symlink() for p in [cache, *cache.parents]):
        raise PipelineError("symlink browser cache paths are not allowed")
    if cache.resolve().is_relative_to(worktree):
        raise PipelineError("shared browser cache must be outside the target worktree")
    cache.mkdir(parents=True, exist_ok=True)
    directory.mkdir(parents=True, exist_ok=True)
    versions = set()
    with (
        slot(host_root() / "playwright-install", execution_directory() or directory, stop, limit=1),
        execution_scope(execution_directory() or directory),
        lock(cache / ".devorchestra-install.lock", wait=True, stop=stop),
    ):
        for key, command in commands:
            check_stop(stop)
            cwd = (
                worktree
                if command.working_directory == "."
                else contained(worktree, command.working_directory)
            )
            try:
                version = installed_version(worktree, cwd)
                if version in versions:
                    continue
                tools = cache / ".devorchestra-installers"
                package = tools / version
                if any(p.is_symlink() for p in [tools, package]):
                    raise PipelineError("symlink installer cache paths are not allowed")
                if package.exists() and not (package / "cli.js").is_file():
                    raise PipelineError("cached Playwright installer is incomplete; inspect cache")
                if not (package / "cli.js").is_file():
                    tools.mkdir(exist_ok=True)
                    with tempfile.TemporaryDirectory(prefix=".install-", dir=tools) as temp:
                        staged = Path(temp) / "package"
                        installer(version, staged, stop)
                        staged.rename(package)
                # A persistent official package also keeps Playwright's client link valid.
                with tempfile.TemporaryDirectory(prefix="devorchestra-playwright-") as temp:
                    scratch = Path(temp).resolve()
                    env = {k: v for k, v in environment().items() if k in {"PATH", "LANG"}}
                    env.update(
                        HOME=str(scratch),
                        TMPDIR=str(scratch),
                        TMP=str(scratch),
                        TEMP=str(scratch),
                        PLAYWRIGHT_BROWSERS_PATH=str(cache),
                        PLAYWRIGHT_SKIP_BROWSER_GC="1",
                    )
                    rc, output = execute(
                        installer_command(Path(node).resolve(), scratch, cache, package),
                        scratch,
                        900,
                        directory / f"{key}-browsers.log",
                        stop=stop,
                        env=env,
                    )
                    if rc:
                        raise PipelineError(f"official installer exited {rc}: {output[-1200:]}")
                versions.add(version)
            except StopRequested:
                raise
            except (OSError, ValueError, KeyError, tarfile.TarError, PipelineError) as error:
                raise PipelineError(
                    sanitize(
                        f"Playwright browser preparation failed for {key}: {error}. "
                        "Check preflight browser logs/network access and resume."
                    )
                ) from error
