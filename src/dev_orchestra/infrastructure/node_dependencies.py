"""Prepare pinned public Node packages; execute installers offline without scripts."""

import hashlib
import json
import platform
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.artifacts import (
    DownloadBudget,
    checksum,
    github_archive,
    public_url,
    verified,
    yarn_entries,
)
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.process import environment, execute
from dev_orchestra.infrastructure.test_runner import sandbox_command


def fetch_entries(entries, directory, stop):
    if len(entries) > 10000:
        raise PipelineError("dependency artifact count exceeds safety limit")
    # Workers download data only; they never invoke repository code.
    budget = DownloadBudget()
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [
            pool.submit(
                github_archive if sri.startswith(("git-", "githubtar-")) else verified,
                url,
                sri,
                directory,
                stop,
                budget,
            )
            for url, sri in entries
        ]
        paths, total = [], 0
        for future in futures:
            path = future.result()
            total += path.stat().st_size
            if total > budget.limit:
                raise PipelineError("dependency archives exceed total safety limit")
            paths.append(path)
        return paths


def prepare_npm_cache(worktree, locked, directory, stop):
    missing = {}
    cache = Path.home() / ".npm"
    os_name = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(platform.system())
    cpu = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "x64", "AMD64": "x64"}.get(
        platform.machine()
    )
    for entry in locked.get("packages", {}).values():
        if not isinstance(entry, dict) or entry.get("link"):
            continue
        if any(
            isinstance(entry.get(key), list)
            and (
                "!" + value in entry[key]
                or (value not in entry[key] and any(not p.startswith("!") for p in entry[key]))
            )
            for key, value in (("os", os_name or ""), ("cpu", cpu or ""))
        ):
            continue
        url, integrity = entry.get("resolved"), entry.get("integrity")
        if not isinstance(url, str) or not isinstance(integrity, str):
            continue
        try:
            public_url(url)
            algorithm, raw = checksum(integrity)
        except PipelineError:
            # Private/local dependencies can still be supplied by an existing offline cache.
            continue
        encoded = raw.hex()
        content = (
            cache / "_cacache/content-v2" / algorithm / encoded[:2] / encoded[2:4] / encoded[4:]
        )
        if content.is_file() and not content.is_symlink() and content.stat().st_size <= 128 * 2**20:
            if hashlib.new(algorithm, content.read_bytes()).digest() == raw:
                continue
        missing[url, integrity] = None
    if not missing:
        return
    tarballs = fetch_entries(list(missing), directory / "artifacts", stop)
    cache.mkdir(parents=True, exist_ok=True)
    if any(p.is_symlink() for p in [cache, *cache.parents]):
        raise PipelineError("npm cache symlinks denied")
    from dev_orchestra.infrastructure.coordination import host_root, slot
    from dev_orchestra.infrastructure.process import execution_directory, execution_scope

    with (
        slot(host_root() / "npm-cache", execution_directory() or directory, stop, limit=1),
        execution_scope(execution_directory() or directory),
        tempfile.TemporaryDirectory(prefix="devorchestra-npm-cache-") as temp,
    ):
        scratch = Path(temp).resolve()
        env = environment() | {
            "HOME": str(scratch),
            "TMPDIR": str(scratch),
            "NPM_CONFIG_USERCONFIG": "/dev/null",
        }
        # Import only checksum-verified local files, not registry URLs; no lifecycle hooks.
        for offset in range(0, len(tarballs), 64):
            argv = [
                "npm",
                "cache",
                "add",
                *(str(p) for p in tarballs[offset : offset + 64]),
                "--offline",
                "--ignore-scripts",
                "--cache",
                str(cache),
                "--logs-dir",
                str(scratch / "logs"),
            ]
            rc, _ = execute(
                sandbox_command(worktree, [cache], scratch, argv),
                scratch,
                180,
                directory / f"npm-cache-{offset}.log",
                stop=stop,
                env=env,
            )
            if rc:
                raise PipelineError("verified offline npm cache import failed; see preflight logs")


def prepare_yarn(worktree, root, package, directory, tracked, stop):
    if package.get("workspaces"):
        raise PipelineError(
            "Yarn workspaces require an explicitly prepared offline dependency tree"
        )
    if not shutil.which("yarn"):
        raise PipelineError(
            "Yarn Classic executable unavailable; install Yarn 1.x once on this host"
        )
    lockfile = contained(worktree, (root / "yarn.lock").relative_to(worktree).as_posix())
    if not lockfile.is_file() or lockfile.stat().st_size > 16 * 2**20:
        raise PipelineError("Yarn Classic requires a bounded committed yarn.lock")
    modules = contained(worktree, (root / "node_modules").relative_to(worktree).as_posix())
    if any((worktree / name).is_relative_to(modules) for name in tracked):
        raise PipelineError("dependency output contains tracked source files")
    key = digest(root.relative_to(worktree).as_posix().encode())[:16]
    marker = directory / f"yarn-{key}.json"
    fingerprint = digest((root / "package.json").read_bytes() + lockfile.read_bytes())
    prior = json.loads(marker.read_text()) if marker.is_file() else {}
    install = not modules.is_dir() or (
        prior and (prior.get("fingerprint") != fingerprint or not prior.get("ready"))
    )
    with tempfile.TemporaryDirectory(prefix="devorchestra-yarn-") as temp:
        scratch = Path(temp).resolve()
        env = environment() | {
            "HOME": str(scratch),
            "TMPDIR": str(scratch),
            "YARN_IGNORE_PATH": "1",
        }
        rc, output = execute(
            sandbox_command(worktree, [], scratch, ["yarn", "--no-default-rc", "--version"]),
            scratch,
            30,
            directory / f"yarn-{key}-version.log",
            stop=stop,
            env=env,
        )
        if rc or not output.strip().startswith("1."):
            raise PipelineError("automatic Yarn preparation requires Yarn Classic 1.x")
        mirror, cache = scratch / "mirror", scratch / "cache"
        mirror.mkdir()
        cache.mkdir()
        config = scratch / ".yarnrc"
        config.write_text(
            "yarn-offline-mirror "
            + json.dumps(str(mirror))
            + "\nyarn-offline-mirror-pruning false\n"
        )
        common = [
            "--offline",
            "--frozen-lockfile",
            "--ignore-scripts",
            "--non-interactive",
            "--no-default-rc",
            "--use-yarnrc",
            str(config),
            "--cache-folder",
            str(cache),
        ]
        if install:
            write(marker, {"fingerprint": fingerprint, "ready": False})
            entries = yarn_entries(lockfile.read_text())
            paths = fetch_entries(
                [(url, sri) for url, sri, _ in entries], directory / "artifacts", stop
            )
            names = {}
            for (_, _, name), path in zip(entries, paths, strict=True):
                if name in names and names[name] != path:
                    raise PipelineError("conflicting Yarn offline mirror filenames")
                names[name] = path
                shutil.copyfile(path, mirror / name)
            modules.mkdir(parents=True, exist_ok=True)
            rc, output = execute(
                sandbox_command(worktree, [modules], scratch, ["yarn", "install", *common]),
                root,
                600,
                directory / f"yarn-{key}-install.log",
                stop=stop,
                env=env,
            )
            if rc:
                raise PipelineError(f"offline Yarn install failed: {output[-1000:]}")
            write(marker, {"fingerprint": fingerprint, "ready": True})
        rc, output = execute(
            sandbox_command(worktree, [], scratch, ["yarn", "check", "--integrity", *common]),
            root,
            60,
            directory / f"yarn-{key}-check.log",
            stop=stop,
            env=env,
        )
        if rc:
            raise PipelineError(f"offline Yarn dependency check failed: {output[-1000:]}")
