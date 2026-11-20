"""Install frozen pnpm workspaces from the host's existing offline store only."""

import json
import re
import shutil
import tempfile
from pathlib import Path

import yaml

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.coordination import host_root, slot
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.process import environment, execute, execution_directory
from dev_orchestra.infrastructure.test_runner import sandbox_command


def pnpm_layout(worktree, root, package):
    declaration = package.get("packageManager", "")
    match = re.fullmatch(r"pnpm@(\d+\.\d+\.\d+)(?:\+sha\d+\.[a-fA-F0-9]+)?", declaration)
    if not match:
        raise PipelineError("pnpm requires an exact packageManager version")
    lockfile = contained(worktree, (root / "pnpm-lock.yaml").relative_to(worktree).as_posix())
    if not lockfile.is_file() or lockfile.stat().st_size > 16 * 2**20:
        raise PipelineError("pnpm requires a bounded committed pnpm-lock.yaml")
    text = lockfile.read_text()
    try:
        if any(isinstance(event, yaml.AliasEvent) for event in yaml.parse(text)):
            raise ValueError("YAML aliases are unsupported")
        locked = yaml.safe_load(text)
        importers = locked["importers"]
        if not isinstance(importers, dict) or "." not in importers or len(importers) > 10000:
            raise ValueError("invalid workspace importers")
    except (yaml.YAMLError, ValueError, KeyError, TypeError) as error:
        raise PipelineError("invalid pnpm workspace lockfile") from error
    manifests, outputs = [lockfile], []
    workspace = contained(worktree, (root / "pnpm-workspace.yaml").relative_to(worktree).as_posix())
    if workspace.is_file():
        manifests.append(workspace)
    for name in importers:
        if not isinstance(name, str):
            raise PipelineError("invalid pnpm workspace importer")
        # Validate before joining: Path('/absolute') would discard the workspace root.
        directory = root if name == "." else contained(root, name)
        relative = directory.relative_to(worktree)
        manifest = contained(worktree, (relative / "package.json").as_posix())
        if not manifest.is_file():
            raise PipelineError("pnpm workspace importer has no package.json")
        manifests.append(manifest)
        outputs.append(contained(worktree, (relative / "node_modules").as_posix()))
    return match[1], manifests, outputs


def prepare_pnpm(worktree, root, package, directory, tracked, stop, *, node_env=None):
    expected, manifests, outputs = pnpm_layout(worktree, root, package)
    if any((worktree / name).is_relative_to(output) for name in tracked for output in outputs):
        raise PipelineError("dependency output contains tracked source files")
    base_env = environment() | (node_env or {})
    executable = shutil.which("pnpm", path=base_env.get("PATH"))
    if not executable:
        raise PipelineError(f"pnpm {expected} unavailable; provision it on the host and resume")
    key = digest(root.relative_to(worktree).as_posix().encode())[:16]
    marker = directory / f"pnpm-{key}.json"

    def source_hash():
        return digest(
            json.dumps(
                [
                    [path.relative_to(worktree).as_posix(), digest(path.read_bytes())]
                    for path in manifests
                ]
            ).encode()
        )

    fingerprint = source_hash()
    with tempfile.TemporaryDirectory(prefix="devorchestra-pnpm-") as temp:
        scratch = Path(temp).resolve()
        env = base_env | {
            "HOME": str(scratch),
            "TMPDIR": str(scratch),
            "TMP": str(scratch),
            "TEMP": str(scratch),
            "NPM_CONFIG_USERCONFIG": "/dev/null",
            "COREPACK_ENABLE_NETWORK": "0",
            "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
            "COREPACK_ENABLE_PROJECT_SPEC": "0",
            "CI": "true",
        }
        rc, output = execute(
            sandbox_command(worktree, [], scratch, [executable, "--version"]),
            scratch,
            30,
            directory / f"pnpm-{key}-version.log",
            stop=stop,
            env=env,
        )
        if rc or output.strip() != expected:
            raise PipelineError(
                f"pnpm {expected} is required; provision the exact host version and resume"
            )
        # Host metadata lookup, outside the repository. A read-only sandbox makes pnpm
        # fall back to a scratch store when probing the normal cache's filesystem.
        rc, output = execute(
            [executable, "store", "path", "--config.ignore-pnpmfile=true"],
            scratch,
            30,
            directory / f"pnpm-{key}-store.log",
            stop=stop,
            env=env | {"HOME": str(Path.home())},
        )
        store = Path(output.strip())
        if (
            rc
            or not store.is_absolute()
            or not store.is_dir()
            or any(path.is_symlink() for path in (store, *store.parents))
            or any(path.is_relative_to(store) for path in (worktree, Path.home(), scratch))
            or store.is_relative_to(worktree)
            or store.is_relative_to(scratch)
        ):
            raise PipelineError("pnpm offline store unavailable; prepare the host store and resume")
        # pnpm's cache can write metadata, so serialize all host-managed writers to this store.
        resource = host_root() / ("pnpm-store-" + digest(str(store).encode())[:16])
        common = [
            "--offline",
            "--frozen-lockfile",
            "--ignore-scripts",
            "--ignore-pnpmfile",
            "--store-dir",
            str(store),
            "--virtual-store-dir",
            str(root / "node_modules/.pnpm"),
            "--config.manage-package-manager-versions=false",
            "--config.pm-on-fail=error",
            "--config.package-import-method=copy",
            "--config.verify-store-integrity=true",
            "--config.side-effects-cache=false",
            "--config.node-linker=isolated",
            "--config.modules-dir=node_modules",
            "--config.enable-global-virtual-store=false",
            "--reporter=append-only",
        ]
        with slot(resource, execution_directory() or directory, stop, limit=1):
            # A repeat frozen install validates the complete graph, including transitive deps.
            # pnpm reuses its content store; no success is inferred from an existing directory.
            write(marker, {"fingerprint": fingerprint, "ready": False, "version": expected})
            rc, output = execute(
                sandbox_command(
                    worktree, [*outputs, store], scratch, [executable, "install", *common]
                ),
                root,
                600,
                directory / f"pnpm-{key}-install.log",
                stop=stop,
                env=env,
            )
            if rc:
                raise PipelineError(
                    "offline pnpm install failed; prepare the pinned dependencies in the host store "
                    f"and resume: {output[-1000:]}"
                )
            if source_hash() != fingerprint:
                raise PipelineError("pnpm changed dependency source files; changes preserved")
            write(marker, {"fingerprint": fingerprint, "ready": True, "version": expected})
