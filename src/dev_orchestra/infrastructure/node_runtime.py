"""Resolve exact project Node pins without installing runtimes or changing the shell."""

import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.process import environment, execute

EXACT_VERSION = re.compile(r"v?(\d+\.\d+\.\d+)")
NODE_TOOLS = {"npm", "npx", "node", "pnpm", "yarn"}


def node_project(worktree, command):
    """Include the host's Python bootstrap when its project introduces Node tooling."""
    bootstrap = Path(__file__).with_name("unittest_bootstrap.py").resolve()
    if Path(command.argv[0]).name not in NODE_TOOLS and not any(
        Path(arg).is_absolute() and Path(arg).resolve() == bootstrap for arg in command.argv[1:]
    ):
        return None
    cwd = (
        worktree
        if command.working_directory == "."
        else contained(worktree, command.working_directory)
    )
    packages = [
        path
        for path in (cwd, *cwd.parents)
        if path.is_relative_to(worktree) and (path / "package.json").is_file()
    ]
    return next(
        (
            path
            for path in packages
            if any(
                (path / name).is_file()
                for name in (
                    "package-lock.json",
                    "yarn.lock",
                    "pnpm-lock.yaml",
                    "pnpm-workspace.yaml",
                )
            )
        ),
        packages[0] if packages else None,
    )


def node_pin(root):
    pins = {}
    for name in (".node-version", ".nvmrc", "package.json"):
        path = contained(root, name)
        if not path.is_file():
            continue
        if path.stat().st_size > 8 * 2**20:
            raise PipelineError(f"Node runtime declaration exceeds safety limit: {name}")
        try:
            value = path.read_text().strip()
            if name == "package.json":
                value = json.loads(value).get("engines", {}).get("node", "")
        except (ValueError, AttributeError) as error:
            raise PipelineError(f"invalid Node runtime declaration: {name}") from error
        if isinstance(value, str) and (match := EXACT_VERSION.fullmatch(value)):
            pins[name] = match[1]
    if len(set(pins.values())) > 1:
        raise PipelineError(
            "conflicting exact Node versions: "
            + ", ".join(f"{name}={version}" for name, version in pins.items())
        )
    return next(iter(pins.values()), None)


def node_environment(root, directory, stop=lambda: None):
    """Return a stable PATH override shared by installers and all test descendants."""
    requested = node_pin(root)
    if requested is None:
        return {}, None
    directory.mkdir(parents=True, exist_ok=True)
    key = digest(str(root).encode())[:16]
    env = environment()
    current = shutil.which("node", path=env.get("PATH"))
    evidence = {"requested_version": requested, "version": None, "executable": None}
    with tempfile.TemporaryDirectory(prefix="devorchestra-node-") as temp:
        scratch = Path(temp).resolve()

        def probe(binary):
            rc, output = execute(
                [
                    str(binary),
                    "-p",
                    "JSON.stringify({version:process.versions.node,executable:process.execPath})",
                ],
                scratch,
                30,
                directory / f"node-{key}-version.log",
                stop=stop,
                env=env,
            )
            if rc:
                raise PipelineError("cannot inspect the selected Node runtime; see preflight logs")
            try:
                result = json.loads(output)
                if not isinstance(result, dict) or not EXACT_VERSION.fullmatch(result["version"]):
                    raise ValueError()
                binary = Path(result["executable"])
                if not binary.is_absolute() or not binary.is_file():
                    raise ValueError()
                return result["version"], binary.resolve()
            except (ValueError, KeyError, TypeError) as error:
                raise PipelineError("invalid Node runtime inspection result") from error

        if current:
            version, binary = probe(current)
            evidence.update(version=version, executable=str(binary))
        if evidence["version"] != requested:
            fnm = shutil.which("fnm", path=env.get("PATH"))
            if fnm:
                # Use only an installed exact version. Never run fnm install or shell init.
                if os.environ.get("FNM_DIR"):
                    env["FNM_DIR"] = os.environ["FNM_DIR"]
                rc, output = execute(
                    [fnm, "exec", "--using=" + requested, "node", "-p", "process.execPath"],
                    scratch,
                    30,
                    directory / f"node-{key}-fnm.log",
                    stop=stop,
                    env=env,
                )
                if rc == 0:
                    candidate = Path(output.strip())
                    if candidate.is_absolute() and candidate.is_file():
                        version, binary = probe(candidate)
                        evidence.update(version=version, executable=str(binary))
        write(directory / f"node-{key}.json", evidence)
        if evidence["version"] != requested:
            raise PipelineError(
                f"Node {requested} is required; host resolved {evidence['version'] or 'no Node'}. "
                f"Install it with fnm install {requested} on the host, then resume. "
                "See preflight Node runtime evidence."
            )
    return {"PATH": str(binary.parent) + os.pathsep + env.get("PATH", "")}, evidence
