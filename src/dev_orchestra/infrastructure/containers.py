"""Host-managed Docker toolchains; repository code runs offline with read-only source."""

import base64
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from dev_orchestra.domain.models import Model, PipelineError
from dev_orchestra.infrastructure import artifacts
from dev_orchestra.infrastructure.coordination import host_root, slot
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.process import environment, execute, execution_directory
from dev_orchestra.infrastructure.secret_files import readable_environment_example, secret_path

MANIFEST = "devorchestra.environment.json"
POLICY = 1
VERSION = r"\d+\.\d+\.\d+"
IMAGE = re.compile(
    r"(?:node|python|postgres|redis|mariadb|debian|ubuntu):[A-Za-z0-9][A-Za-z0-9_.-]{0,100}"
)


class ContainerEnvironment(Model):
    schema_version: Literal[1] = 1
    backend: Literal["docker"] = "docker"
    image: str
    node: str | None = None
    pnpm: str | None = None
    packages: list[str] = Field(default_factory=list, max_length=32)


def specification(worktree):
    path = contained(worktree, MANIFEST)
    if not path.is_file():
        return None
    if path.stat().st_size > 65536:
        raise PipelineError("container environment manifest exceeds safety limit")
    try:
        spec = ContainerEnvironment.model_validate_json(path.read_text())
    except (ValidationError, UnicodeError) as error:
        raise PipelineError("invalid container environment manifest") from error
    if not IMAGE.fullmatch(spec.image) or ":latest" in spec.image:
        raise PipelineError("container environment requires a versioned official toolchain image")
    if any(
        value is not None and not re.fullmatch(VERSION, value) for value in (spec.node, spec.pnpm)
    ):
        raise PipelineError("container Node and pnpm versions must be exact")
    if any(
        not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?:=[A-Za-z0-9.+:~_-]+)?", value)
        for value in spec.packages
    ):
        raise PipelineError("unsupported container system package declaration")
    return spec


def local_docker(directory, stop=lambda: None):
    binary = shutil.which("docker")
    if not binary:
        raise PipelineError("Docker CLI unavailable; install a local container runtime")
    directory.mkdir(parents=True, exist_ok=True)
    rc, output = execute(
        [binary, "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
        directory,
        30,
        directory / "docker-context.log",
        stop=stop,
    )
    try:
        endpoint = json.loads(output)
        socket = Path(endpoint.removeprefix("unix://"))
        if rc or not endpoint.startswith("unix://") or not socket.is_absolute():
            raise ValueError()
        info = socket.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid not in {0, os.getuid()}:
            raise ValueError()
    except (ValueError, TypeError, AttributeError, OSError) as error:
        raise PipelineError(
            "local Docker daemon unavailable; start Colima/Docker and resume"
        ) from error
    config = directory / "docker-config"
    config.mkdir(exist_ok=True)
    return [binary, "--host", endpoint, "--config", str(config)]


def command(base, args, directory, label, stop=lambda: None, timeout=120, stdin=None, env=None):
    rc, output = execute(
        [*base, *args],
        directory,
        timeout,
        directory / (label + ".log"),
        stop=stop,
        stdin=stdin,
        env=env,
    )
    if rc:
        raise PipelineError(
            f"container {label} failed; see environment preparation logs: {output[-1200:]}"
        )
    return output.strip()


def immutable_image(base, reference, directory, stop):
    rc, output = execute(
        [*base, "image", "inspect", "--format", "{{.Id}}", reference],
        directory,
        30,
        directory / "docker-image.log",
        stop=stop,
    )
    if rc:
        command(base, ["pull", reference], directory, "docker-pull", stop, timeout=600)
        output = command(
            base,
            ["image", "inspect", "--format", "{{.Id}}", reference],
            directory,
            "docker-image",
            stop,
        )
    value = output.strip()
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", value):
        raise PipelineError("Docker image identity unavailable")
    return value


def toolchain(worktree, directory, stop=lambda: None):
    spec = specification(worktree)
    if spec is None:
        return None
    base = local_docker(directory, stop)
    image = immutable_image(base, spec.image, directory, stop)
    key = digest(json.dumps([POLICY, spec.model_dump(), image, os.getuid(), os.getgid()]).encode())
    tag = "devorchestra/toolchain:" + key
    with slot(host_root() / "docker-toolchains", execution_directory() or directory, stop, limit=1):
        rc, output = execute(
            [*base, "image", "inspect", "--format", "{{.Id}}", tag],
            directory,
            30,
            directory / "toolchain-inspect.log",
            stop=stop,
        )
        if rc:
            context = directory / ("toolchain-" + key[:16])
            context.mkdir(exist_ok=True)
            lines = [
                f"FROM {image}",
                "USER root",
                "ENV DEBIAN_FRONTEND=noninteractive",
                "RUN apt-get update && apt-get install -y --no-install-recommends "
                + " ".join(["python3", "ca-certificates", "xz-utils", "passwd", *spec.packages])
                + " && rm -rf /var/lib/apt/lists/*",
            ]
            if spec.node:
                arch = command(
                    base,
                    ["version", "--format", "{{.Server.Arch}}"],
                    directory,
                    "docker-arch",
                    stop,
                )
                arch = {"arm64": "arm64", "aarch64": "arm64", "amd64": "x64", "x86_64": "x64"}.get(
                    arch
                )
                if not arch:
                    raise PipelineError("unsupported Docker Node architecture")
                filename = f"node-v{spec.node}-linux-{arch}.tar.xz"
                url = f"https://nodejs.org/dist/v{spec.node}/"
                sums = artifacts.fetch(url + "SHASUMS256.txt", stop, limit=1048576).decode()
                match = re.search(r"(?m)^([a-f0-9]{64})\s+" + re.escape(filename) + r"$", sums)
                if not match:
                    raise PipelineError("official Node archive checksum unavailable")
                integrity = "sha256-" + base64.b64encode(bytes.fromhex(match[1])).decode()
                archive = artifacts.verified(url + filename, integrity, context / "artifacts", stop)
                shutil.copyfile(archive, context / "node.tar.xz")
                lines += [
                    "COPY node.tar.xz /tmp/node.tar.xz",
                    "RUN mkdir /opt/node && tar -xJf /tmp/node.tar.xz --strip-components=1 -C /opt/node && rm /tmp/node.tar.xz",
                    "ENV PATH=/opt/node/bin:$PATH",
                ]
            if spec.pnpm:
                metadata = json.loads(
                    artifacts.fetch(
                        "https://registry.npmjs.org/pnpm/" + spec.pnpm, stop, limit=1048576
                    )
                )
                if metadata.get("name") != "pnpm" or metadata.get("version") != spec.pnpm:
                    raise PipelineError("pnpm metadata version mismatch")
                archive = artifacts.verified(
                    metadata["dist"]["tarball"],
                    metadata["dist"]["integrity"],
                    context / "artifacts",
                    stop,
                )
                shutil.copyfile(archive, context / "pnpm.tgz")
                lines += [
                    "COPY pnpm.tgz /tmp/pnpm.tgz",
                    "RUN mkdir /opt/pnpm && tar -xzf /tmp/pnpm.tgz --strip-components=1 -C /opt/pnpm && rm /tmp/pnpm.tgz && printf '#!/bin/sh\\nexec node /opt/pnpm/bin/pnpm.cjs \"$@\"\\n' > /usr/local/bin/pnpm && chmod 755 /usr/local/bin/pnpm",
                ]
            lines += [
                f"RUN getent group {os.getgid()} >/dev/null || groupadd -g {os.getgid()} devorchestra",
                f"RUN getent passwd {os.getuid()} >/dev/null || useradd -u {os.getuid()} -g {os.getgid()} -d /tmp devorchestra",
                "ENTRYPOINT []",
            ]
            write(context / "Dockerfile", "\n".join(lines) + "\n")
            write(context / ".dockerignore", "artifacts/\n")
            # This context contains host recipes and verified tool archives, never project source.
            command(
                base,
                ["build", "--tag", tag, str(context)],
                directory,
                "toolchain-build",
                stop,
                timeout=900,
            )
            output = command(
                base,
                ["image", "inspect", "--format", "{{.Id}}", tag],
                directory,
                "toolchain-inspect",
                stop,
            )
        selected = output.strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", selected):
            raise PipelineError("prepared Docker toolchain identity unavailable")
    return {
        "image_id": selected,
        "base_image_id": image,
        "configuration_hash": key,
        "specification": spec.model_dump(),
        "docker": base,
    }


def run_arguments(
    worktree,
    source,
    outputs,
    scratch,
    argv,
    prepared,
    directory,
    *,
    network=False,
    cwd=None,
    extra_env=None,
    adapters=(),
):
    """Only trusted dependency installation gets network, with a manifest-only source tree."""
    if any(
        any(char in str(path) for char in (",", "\n", "\r"))
        for path in (worktree, source, *outputs, *adapters)
    ):
        raise PipelineError("unsupported comma or newline in container mount path")
    name = "devorchestra-" + uuid.uuid4().hex
    token = uuid.uuid4().hex
    owner = directory / (name + ".owner.json")
    cid = directory / (name + ".cid")
    write(
        owner,
        {
            "token": token,
            "name": name,
            "image_id": prepared["image_id"],
            "docker": prepared["docker"],
            "cidfile": str(cid),
        },
    )
    command_argv = [*argv]
    bootstrap = Path(__file__).with_name("unittest_bootstrap.py").resolve()
    if len(command_argv) == 2 and Path(command_argv[1]).resolve() == bootstrap:
        command_argv = ["python3", "/opt/devorchestra/unittest_bootstrap.py"]
    args = [
        *prepared["docker"],
        "run",
        "--rm",
        "--name",
        name,
        "--cidfile",
        str(cid),
        "--label",
        "devorchestra.owner=" + token,
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=256",
        "--memory=2g",
        "--cpus=2",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--tmpfs",
        "/tmp:rw,nosuid,size=1g,mode=1777",
        "--network",
        "bridge" if network else "none",
        "--entrypoint",
        "/usr/bin/env",
        "--mount",
        f"type=bind,source={source},target={worktree},readonly",
        "--mount",
        f"type=bind,source={bootstrap},target=/opt/devorchestra/unittest_bootstrap.py,readonly",
    ]
    for output in outputs:
        args += ["--mount", f"type=bind,source={output},target={output}"]
    for path in adapters:
        args += ["--mount", f"type=bind,source={path},target={path},readonly"]
    if not network:
        for path in worktree.rglob("*"):
            relative = path.relative_to(worktree)
            if (
                secret_path(relative)
                and not path.is_symlink()
                and not readable_environment_example(path, relative)
            ):
                if any(char in str(path) for char in (",", "\n", "\r")):
                    raise PipelineError("unsupported secret mount path")
                if path.is_file():
                    args += ["--mount", f"type=bind,source=/dev/null,target={path},readonly"]
                elif path.is_dir():
                    args += ["--tmpfs", f"{path}:rw,nosuid,size=1m,mode=000"]
    args += [
        "--workdir",
        str(cwd or worktree),
        prepared["image_id"],
        "HOME=/tmp",
        "TMPDIR=/tmp",
        "PYTHONDONTWRITEBYTECODE=1",
        "COREPACK_ENABLE_NETWORK=0",
        "JAVA_TOOL_OPTIONS=-Duser.home=/tmp -Djava.io.tmpdir=/tmp",
        *(key + "=" + value for key, value in (extra_env or {}).items()),
        *command_argv,
    ]
    return args, {"DEVORCHESTRA_CONTAINER_OWNER": str(owner)}


def container_node_projects(worktree, commands, *, include_root=False):
    from dev_orchestra.infrastructure.dependencies import read_package
    from dev_orchestra.infrastructure.node_runtime import node_project, package_manager

    def reject_yarn(location):
        raise PipelineError(
            f"{location}: Docker dependency preparation does not support Yarn; "
            "keep the existing host Yarn validation and omit the Docker environment declaration"
        )

    roots = {}
    for command_id, item in commands.items():
        if Path(item.argv[0]).name == "yarn":
            reject_yarn(f"{command_id} ({item.working_directory})")
        if root := node_project(worktree, item):
            roots[root] = f"{command_id} ({root.relative_to(worktree)})"
    if include_root and not commands and contained(worktree, "package.json").is_file():
        roots[worktree] = "."
    projects = {}
    for root, location in roots.items():
        package = read_package(contained(root, "package.json"))
        manager = package_manager(root, package)
        if manager == "yarn":
            reject_yarn(location)
        if manager not in {"npm", "pnpm"}:
            raise PipelineError("container dependency preparation currently supports npm and pnpm")
        projects[root] = package, manager
    return projects


def prepare_dependencies(worktree, config, directory, stop=lambda: None, *, generate_locks=False):
    """Online installers see copied manifests only; their repository hooks are unavailable."""
    if not contained(worktree, MANIFEST).is_file():
        return None
    projects = container_node_projects(worktree, config.commands, include_root=generate_locks)
    prepared = toolchain(worktree, directory, stop)
    from dev_orchestra.infrastructure.git import Git
    from dev_orchestra.infrastructure.node_runtime import node_pin

    generated = {}
    tracked = [name for name in Git(worktree).call("ls-files", "-z").split("\0") if name]
    for root, (package, manager) in projects.items():
        pin = node_pin(root)
        if pin and pin != prepared["specification"]["node"]:
            raise PipelineError("project Node version conflicts with container environment")
        if manager == "pnpm" and package.get("packageManager") != "pnpm@" + (
            prepared["specification"]["pnpm"] or ""
        ):
            raise PipelineError("packageManager must match the declared container pnpm version")
        lockname = "pnpm-lock.yaml" if manager == "pnpm" else "package-lock.json"
        lockfile = contained(root, lockname)
        if not lockfile.is_file() and not generate_locks:
            raise PipelineError(
                "missing installation-generated lockfile; Developer must call workspace.prepare_environment"
            )
        view = directory / ("dependency-manifests-" + uuid.uuid4().hex)
        view.mkdir()
        excluded = [contained(worktree, name) for name in config.output_paths]
        files = [
            p
            for p in root.rglob("package.json")
            if "node_modules" not in p.relative_to(root).parts
            and not any(p.is_relative_to(output) for output in excluded)
        ]
        if len(files) > 1000:
            raise PipelineError("too many workspace manifests")
        outputs = []
        total = 0
        for original in [*files, *(root / name for name in (lockname, "pnpm-workspace.yaml"))]:
            if not original.is_file():
                continue
            original = contained(worktree, original.relative_to(worktree).as_posix())
            total += original.stat().st_size
            if total > 32 * 2**20:
                raise PipelineError("dependency manifests exceed safety limit")
            destination = contained(view, original.relative_to(worktree).as_posix())
            destination.parent.mkdir(parents=True, exist_ok=True)
            content = original.read_text()
            if original.name == "package.json":
                value = json.loads(content)
                pnpm_settings = value.get("pnpm", {})
                if not isinstance(pnpm_settings, dict) or set(pnpm_settings) - {"overrides"}:
                    raise PipelineError(
                        "unsupported package manager hooks or settings in online preparation"
                    )

                def verify_overrides(item):
                    if isinstance(item, dict):
                        for nested in item.values():
                            verify_overrides(nested)
                    elif not isinstance(item, str) or re.match(
                        r"(?:file:|https?:|git|ssh:|link:|/|\.)", item
                    ):
                        raise PipelineError("non-registry dependency override is unsupported")

                verify_overrides(value.get("overrides", {}))
                verify_overrides(pnpm_settings.get("overrides", {}))
                for section in (
                    "dependencies",
                    "devDependencies",
                    "optionalDependencies",
                    "peerDependencies",
                ):
                    for version in value.get(section, {}).values():
                        if not isinstance(version, str) or re.match(
                            r"(?:file:|https?:|git|ssh:|link:|/|\.)", version
                        ):
                            raise PipelineError(
                                "container preparation supports registry packages and workspace links only"
                            )
                allowed = {
                    "name",
                    "version",
                    "private",
                    "type",
                    "workspaces",
                    "dependencies",
                    "devDependencies",
                    "optionalDependencies",
                    "peerDependencies",
                    "peerDependenciesMeta",
                    "engines",
                    "packageManager",
                    "os",
                    "cpu",
                    "overrides",
                    "pnpm",
                    "exports",
                    "main",
                    "module",
                    "types",
                }
                content = json.dumps({key: item for key, item in value.items() if key in allowed})
            elif original.name == "pnpm-workspace.yaml":
                import yaml

                value = yaml.safe_load(content)
                if not isinstance(value, dict) or set(value) - {
                    "packages",
                    "catalog",
                    "catalogs",
                    "overrides",
                }:
                    raise PipelineError(
                        "unsupported pnpm workspace settings in container preparation"
                    )
                if re.search(r"(?:https?://|git\+|file:|ssh:)", content):
                    raise PipelineError("non-registry workspace dependencies are unsupported")
            else:
                import yaml

                locked = (
                    json.loads(content) if original.suffix == ".json" else yaml.safe_load(content)
                )

                def verify_urls(value):
                    if isinstance(value, dict):
                        for key, item in value.items():
                            if (
                                key in {"resolved", "tarball"}
                                and isinstance(item, str)
                                and not value.get("link")
                            ):
                                artifacts.public_url(item)
                            else:
                                verify_urls(item)
                    elif isinstance(value, list):
                        for item in value:
                            verify_urls(item)

                verify_urls(locked)
            write(destination, content)
            if original.name == "package.json":
                output = contained(
                    worktree, original.parent.relative_to(worktree).as_posix() + "/node_modules"
                )
                if any((worktree / name).is_relative_to(output) for name in tracked):
                    raise PipelineError("dependency output contains tracked source files")
                output.mkdir(exist_ok=True)
                (destination.parent / "node_modules").mkdir(exist_ok=True)
                outputs.append(output)
        # Installers may write only this disposable manifest tree and dependency outputs.
        # No source, .npmrc, pnpmfile, user config, credentials or Docker socket is mounted.
        local_root = (
            contained(view, root.relative_to(worktree).as_posix()) if root != worktree else view
        )
        argv = (
            [
                "pnpm",
                "install",
                "--ignore-scripts",
                "--ignore-pnpmfile",
                "--config.manage-package-manager-versions=false",
                "--config.pm-on-fail=error",
                "--reporter=append-only",
                "--registry=https://registry.npmjs.org",
            ]
            if manager == "pnpm"
            else [
                "npm",
                "ci" if lockfile.is_file() and not generate_locks else "install",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--registry=https://registry.npmjs.org",
            ]
        )
        if manager == "pnpm":
            argv += (
                ["--frozen-lockfile"]
                if lockfile.is_file() and not generate_locks
                else ["--no-frozen-lockfile"]
            )
        # The temporary manifest tree is a preparation output, never the worktree source.
        args, owner_env = run_arguments(
            worktree, view, outputs, view, argv, prepared, directory, network=True, cwd=root
        )
        mount = f"type=bind,source={view},target={worktree},readonly"
        args[args.index(mount)] = f"type=bind,source={view},target={worktree}"
        command(
            [],
            args,
            directory,
            "container-dependencies",
            stop,
            timeout=600,
            env=environment() | owner_env,
        )
        result = local_root / lockname
        if not result.is_file():
            raise PipelineError("container installer did not produce a lockfile")
        if generate_locks and (
            not lockfile.is_file() or result.read_bytes() != lockfile.read_bytes()
        ):
            generated[(root / lockname).relative_to(worktree).as_posix()] = result.read_text()
        elif result.read_bytes() != lockfile.read_bytes():
            raise PipelineError("container installer changed the frozen lockfile")
    write(
        directory / "container-environment.json",
        {key: value for key, value in prepared.items() if key != "docker"},
    )
    return prepared | {"generated_lockfiles": generated}


def cleanup_owned_container(path):
    """The process guardian authenticates container ownership before terminating anything."""
    data = json.loads(Path(path).read_text())
    base, name = data["docker"], data["name"]
    if not re.fullmatch(r"devorchestra-[a-f0-9]{32}", name):
        raise PipelineError("invalid owned container name")
    result = subprocess.run(
        [*base, "inspect", "--format", "{{json .}}", name],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        if not any(
            value in result.stderr.lower() for value in ("no such object", "no such container")
        ):
            raise PipelineError(
                "cannot verify owned container removal: " + result.stderr.strip()[:300]
            )
        # Confirm the daemon is reachable; an unavailable daemon is not proof of removal.
        subprocess.run(
            [*base, "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        return
    info = json.loads(result.stdout)
    if (
        info["Config"]["Labels"].get("devorchestra.owner") != data["token"]
        or info["Image"] != data["image_id"]
    ):
        raise PipelineError("container ownership mismatch; container preserved")
    subprocess.run(
        [*base, "rm", "--force", info["Id"]], capture_output=True, check=True, timeout=10
    )


def host_tools(directory, stop=lambda: None):
    """Report observed versions so a new plan can reuse compatible installed tools."""
    directory.mkdir(parents=True, exist_ok=True)
    values = {}
    for name in ("node", "pnpm", "fnm", "postgres", "docker", "colima"):
        binary = shutil.which(name)
        row = {"available": bool(binary), "version": None}
        if binary:
            rc, output = execute(
                [binary, "--version"],
                directory,
                30,
                directory / (name + "-available.log"),
                stop=stop,
                env=environment()
                | {"COREPACK_ENABLE_NETWORK": "0", "COREPACK_ENABLE_PROJECT_SPEC": "0"},
            )
            match = re.search(r"(?<![\w.])v?(\d+\.\d+(?:\.\d+)?)\b", output)
            row.update(available=rc == 0, version=match[1] if rc == 0 and match else None)
        values[name] = row
    if values["docker"]["available"]:
        try:
            base = local_docker(directory, stop)
            command(
                base,
                ["version", "--format", "{{.Server.Version}}"],
                directory,
                "docker-ready",
                stop,
                timeout=30,
            )
            values["docker"]["ready"] = True
        except PipelineError:
            values["docker"]["ready"] = False
    values["container_manifest"] = MANIFEST
    write(directory / "host-tools.json", values)
    return values
