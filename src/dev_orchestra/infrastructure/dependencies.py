"""Prepare locked npm dependencies offline, without executing install scripts."""

import json
import re
import tempfile
from pathlib import Path

from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.java import prepare_java
from dev_orchestra.infrastructure.node_dependencies import prepare_npm_cache, prepare_yarn
from dev_orchestra.infrastructure.playwright import browser_cache, prepare_browsers
from dev_orchestra.infrastructure.process import environment, execute, sanitize
from dev_orchestra.infrastructure.test_runner import sandbox_command


def vite_cache_output(package, prefix, files, script=None):
    scripts = package.get("scripts", {})
    values = [scripts.get(script, "")] if script else scripts.values()
    if any(
        isinstance(value, str) and re.search(r"(?:^|\s)vite(?:\s|$)", value) for value in values
    ) and any(
        prefix + "vite.config." + ext in files for ext in ("js", "ts", "mjs", "mts", "cjs", "cts")
    ):
        return prefix + "node_modules/.vite-temp"
    return None


def read_package(path):
    try:
        if path.stat().st_size > 8 * 1048576:
            raise ValueError("manifest exceeds safety limit")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError("manifest must be an object")
        return value
    except (OSError, ValueError) as error:
        raise PipelineError(f"cannot read dependency manifest {path.name}: {error}") from error


def prepare_dependencies(worktree, config, directory, stop=lambda: None):
    worktree = worktree.resolve()
    prepare_java(worktree, config, directory, stop)
    roots = {}
    for command_id, command in config.commands.items():
        browsers = command.env.get("PLAYWRIGHT_BROWSERS_PATH")
        if browsers and browsers not in {"0", str(browser_cache())}:
            cwd = (
                worktree
                if command.working_directory == "."
                else contained(worktree, command.working_directory)
            )
            browser_path = Path(browsers)
            if not browser_path.is_absolute():
                browser_path = cwd / browser_path
            if not browser_path.is_dir():
                raise PipelineError(
                    f"Playwright browsers unavailable for {command_id} "
                    f"(working directory: {command.working_directory}): "
                    f"PLAYWRIGHT_BROWSERS_PATH directory does not exist: {browsers}. "
                    "Prepare browsers at the configured path and resume."
                )
        tool = Path(command.argv[0]).name
        if tool not in {"npm", "npx", "node", "pnpm", "yarn"}:
            continue
        cwd = (
            worktree
            if command.working_directory == "."
            else contained(worktree, command.working_directory)
        )
        candidates = [cwd, *cwd.parents]
        candidates = [p for p in candidates if p.is_relative_to(worktree)]
        packages = [p for p in candidates if (p / "package.json").exists()]
        if not packages:
            continue
        root = next(
            (
                p
                for p in packages
                if any(
                    (p / lock).exists()
                    for lock in ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")
                )
            ),
            packages[0],
        )
        roots.setdefault(root, []).append(command_id)
    if not roots:
        prepare_browsers(worktree, config, directory, stop)
        return
    directory.mkdir(parents=True, exist_ok=True)
    tracked = [name for name in Git(worktree).call("ls-files", "-z").split("\0") if name]
    for root, command_ids in roots.items():
        label = root.relative_to(worktree).as_posix()
        hint = (
            f"Node dependencies unavailable for {', '.join(command_ids)} "
            f"(working directory: {label}). Prepare dependencies in this run's worktree "
            "and resume; see preflight dependency logs."
        )
        try:
            manifest = contained(worktree, (root / "package.json").relative_to(worktree).as_posix())
            package = read_package(manifest)
            for command_id in command_ids:
                command = config.commands[command_id]
                if "run" in command.argv:
                    position = command.argv.index("run") + 1
                    script = command.argv[position] if position < len(command.argv) else None
                    cwd = (
                        worktree
                        if command.working_directory == "."
                        else contained(worktree, command.working_directory)
                    )
                    cwd = next(
                        p
                        for p in [cwd, *cwd.parents]
                        if p.is_relative_to(worktree) and (p / "package.json").exists()
                    )
                    local = read_package(
                        contained(worktree, (cwd / "package.json").relative_to(worktree).as_posix())
                    )
                    prefix = "" if cwd == worktree else cwd.relative_to(worktree).as_posix() + "/"
                    cache = vite_cache_output(local, prefix, tracked, script)
                    if (
                        not cache
                        and script
                        and re.search(
                            r"(?:^|\s)vue-cli-service\s+build(?:\s|$)",
                            local.get("scripts", {}).get(script, ""),
                        )
                    ):
                        cache = prefix + "node_modules/.cache"
                    if cache and not any(
                        Path(cache).is_relative_to(Path(p)) for p in config.output_paths
                    ):
                        raise PipelineError(
                            f"{command_id} requires Node build temporary output {cache}. "
                            "Add it to project tests.output_paths and create a new run; "
                            "existing run validation policy is immutable."
                        )
            if not any(package.get(k) for k in ("dependencies", "devDependencies", "workspaces")):
                continue
            modules = contained(worktree, (root / "node_modules").relative_to(worktree).as_posix())
            if modules.exists() and not modules.is_dir():
                raise PipelineError("node_modules must be a directory")
            lockfile = contained(
                worktree, (root / "package-lock.json").relative_to(worktree).as_posix()
            )
            manager = package.get(
                "packageManager", "yarn" if (root / "yarn.lock").is_file() else "npm"
            ).split("@", 1)[0]
            if manager == "yarn":
                prepare_yarn(worktree, root, package, directory, tracked, stop)
                continue
            if manager != "npm" or not lockfile.is_file():
                if modules.is_dir():
                    continue
                raise PipelineError("automatic preparation requires npm and package-lock.json")
            locked = read_package(lockfile)
            if not isinstance(locked.get("packages", {}), dict):
                raise PipelineError("lockfile packages must be an object")
            outputs = [modules]
            manifests = [manifest, lockfile]
            # npm workspaces may install nested dependencies as well as root dependencies.
            for name, entry in locked.get("packages", {}).items():
                if not isinstance(entry, dict):
                    raise PipelineError("invalid lockfile package entry")
                if not name or "node_modules" in Path(name).parts or entry.get("link"):
                    continue
                workspace = contained(worktree, (root / name).relative_to(worktree).as_posix())
                manifests.append(
                    contained(
                        worktree, (workspace / "package.json").relative_to(worktree).as_posix()
                    )
                )
                outputs.append(
                    contained(
                        worktree, (workspace / "node_modules").relative_to(worktree).as_posix()
                    )
                )
            if any((worktree / name).is_relative_to(p) for name in tracked for p in outputs):
                raise PipelineError("dependency output contains tracked source files")
            fingerprint = digest(
                json.dumps(
                    [
                        [p.relative_to(worktree).as_posix(), digest(p.read_bytes())]
                        for p in manifests
                    ]
                ).encode()
            )
            key = digest(label.encode())[:16]
            marker = directory / f"dependencies-{key}.json"
            install = not modules.exists()
            if marker.exists() and modules.exists():
                previous = read_package(marker)
                install = previous["fingerprint"] != fingerprint or not previous.get("ready")
            with tempfile.TemporaryDirectory(prefix="devorchestra-dependencies-") as temp:
                scratch = Path(temp).resolve()
                env = environment() | {
                    "HOME": str(scratch),
                    "TMPDIR": str(scratch),
                    "TMP": str(scratch),
                    "TEMP": str(scratch),
                    "NPM_CONFIG_USERCONFIG": "/dev/null",
                }
                common = [
                    "--offline",
                    "--ignore-scripts",
                    "--no-audit",
                    "--no-fund",
                    "--include=dev",
                    "--include=optional",
                    "--cache",
                    str(Path.home() / ".npm"),
                    "--logs-dir",
                    str(scratch / "logs"),
                    "--update-notifier=false",
                ]
                if install:
                    write(
                        marker,
                        {"fingerprint": fingerprint, "working_directory": label, "ready": False},
                    )
                    for output in outputs:
                        output.mkdir(parents=True, exist_ok=True)
                    prepare_npm_cache(worktree, locked, directory, stop)
                    argv = sandbox_command(worktree, outputs, scratch, ["npm", "ci", *common])
                    rc, output = execute(
                        argv,
                        root,
                        600,
                        directory / f"dependencies-{key}-install.log",
                        stop=stop,
                        env=env,
                    )
                    if rc:
                        raise PipelineError(f"offline npm ci exited {rc}: {output[-1200:].strip()}")
                    write(
                        marker,
                        {"fingerprint": fingerprint, "working_directory": label, "ready": True},
                    )
                argv = sandbox_command(worktree, [], scratch, ["npm", "ls", "--depth=0", *common])
                rc, output = execute(
                    argv, root, 60, directory / f"dependencies-{key}-check.log", stop=stop, env=env
                )
                if rc:
                    raise PipelineError(
                        f"npm dependency check exited {rc}: {output[-1200:].strip()}"
                    )
        except StopRequested:
            raise
        except (PipelineError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise PipelineError(sanitize(f"{hint} {error}")) from error
    prepare_browsers(worktree, config, directory, stop)
