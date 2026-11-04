"""Resolve literal public protobuf compiler tools without online Gradle execution."""

import base64
import hashlib
import json
import platform
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.artifacts import fetch, verified
from dev_orchestra.infrastructure.files import contained
from dev_orchestra.infrastructure.git import Git

TOOLS = {("com.google.protobuf", "protoc"): "protoc", ("io.grpc", "protoc-gen-grpc-java"): "grpc"}


def declarations(worktree):
    result = []
    for name in Git(worktree).call("ls-files", "-z").split("\0"):
        if not name.endswith(("build.gradle", "build.gradle.kts")):
            continue
        path = contained(worktree, name)
        if path.stat().st_size > 2**20:
            continue
        text = path.read_text()
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"(?m)^\s*//.*$", "", text)
        for group, artifact, version in re.findall(
            r"""\bartifact\s*=\s*["']([\w.]+):([\w-]+):(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)["']""",
            text,
        ):
            if (group, artifact) in TOOLS:
                result.append((str(Path(name).parent), group, artifact, version))
    return list(dict.fromkeys(result))


def classifier():
    system = {"Darwin": "osx", "Linux": "linux", "Windows": "windows"}.get(platform.system())
    cpu = {"arm64": "aarch_64", "aarch64": "aarch_64", "x86_64": "x86_64", "AMD64": "x86_64"}.get(
        platform.machine()
    )
    if not system or not cpu:
        raise PipelineError("public protobuf native compiler platform unsupported")
    return system + "-" + cpu


def tool_root(worktree, command):
    from dev_orchestra.infrastructure.java import cache_paths

    return contained(
        worktree,
        str(Path(command.working_directory) / cache_paths(command)[0] / "devorchestra-tools"),
    )


def project_checksum(worktree, group, artifact, version, name):
    paths = [
        p
        for p in Git(worktree).call("ls-files", "-z").split("\0")
        if p.endswith("gradle/verification-metadata.xml")
    ]
    if not paths:
        return None
    for path in paths:
        file = contained(worktree, path)
        if file.stat().st_size > 8 * 2**20:
            raise PipelineError("Gradle verification metadata oversized")
        text = file.read_text()
        if "<!DOCTYPE" in text or "<!ENTITY" in text:
            raise PipelineError("XML declarations denied in Gradle verification metadata")
        tree = ET.fromstring(text)
        for element in tree.iter():
            element.tag = element.tag.rsplit("}", 1)[-1]
        for component in tree.iter("component"):
            if (component.get("group"), component.get("name"), component.get("version")) != (
                group,
                artifact,
                version,
            ):
                continue
            for item in component.findall("artifact"):
                if item.get("name") == name:
                    for algorithm in ("sha512", "sha256", "sha1"):
                        for value in item.findall(algorithm):
                            raw = value.get("value", "")
                            if (
                                re.fullmatch(r"[a-fA-F0-9]+", raw)
                                and len(raw) == hashlib.new(algorithm).digest_size * 2
                            ):
                                return (
                                    algorithm + "-" + base64.b64encode(bytes.fromhex(raw)).decode()
                                )
    raise PipelineError(
        "native compiler requires a matching checksum in existing Gradle verification metadata"
    )


def prepare_native_tools(worktree, command, directory, stop):
    entries = declarations(worktree)
    if not entries:
        return
    root = tool_root(worktree, command)
    root.mkdir(parents=True, exist_ok=True)
    for project, group, artifact, version in entries:
        name = f"{artifact}-{version}-{classifier()}.exe"
        target = contained(worktree, str((root / name).relative_to(worktree)))
        marker = contained(worktree, str(target.with_suffix(".json").relative_to(worktree)))
        pinned = project_checksum(worktree, group, artifact, version, name)
        if (
            target.is_file()
            and marker.is_file()
            and target.stat().st_size <= 128 * 2**20
            and marker.stat().st_size <= 2**20
        ):
            recorded = json.loads(marker.read_text())
            if (
                hashlib.sha256(target.read_bytes()).hexdigest() == recorded.get("sha256")
                and recorded.get("project_integrity") == pinned
            ):
                check_native_cpu(worktree, command, project, target, directory, stop)
                continue
        url = (
            "https://repo.maven.apache.org/maven2/"
            + group.replace(".", "/")
            + f"/{artifact}/{version}/{name}"
        )
        integrity = pinned
        if integrity is None:
            try:
                value = fetch(url + ".sha256", stop, 1024).decode().strip()
                algorithm = "sha256"
            except PipelineError:
                value = fetch(url + ".sha1", stop, 1024).decode().strip()
                algorithm = "sha1"
            if (
                not re.fullmatch(r"[a-fA-F0-9]+", value)
                or len(value) != hashlib.new(algorithm).digest_size * 2
            ):
                raise PipelineError("official native compiler checksum invalid")
            integrity = algorithm + "-" + base64.b64encode(bytes.fromhex(value)).decode()
        source = verified(url, integrity, directory / "artifacts", stop)
        target.write_bytes(source.read_bytes())
        target.chmod(0o755)
        marker.write_text(
            json.dumps(
                {
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "integrity": integrity,
                    "project_integrity": pinned,
                }
            )
        )
        check_native_cpu(worktree, command, project, target, directory, stop)


def check_native_cpu(worktree, command, project, target, directory, stop):
    """Detect mislabelled historical ARM artifacts before model/repair work."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return
    with target.open("rb") as file:
        header = file.read(8)
    if header != bytes.fromhex("cffaedfe07000001"):
        return
    prefix = ":" + project.replace("/", ":").strip(".:") + ":"
    root_goals = {
        "assemble",
        "build",
        "check",
        "test",
        "classes",
        "compileJava",
        "generateProto",
        "jar",
        "bootJar",
    }
    if project != "." and not any(
        arg in root_goals or arg.startswith(prefix) for arg in command.argv[1:]
    ):
        return
    import tempfile

    from dev_orchestra.infrastructure.process import environment, execute
    from dev_orchestra.infrastructure.test_runner import sandbox_command

    with tempfile.TemporaryDirectory(prefix="devorchestra-rosetta-check-") as temp:
        scratch = Path(temp).resolve()
        rc, _ = execute(
            sandbox_command(worktree, [], scratch, ["/usr/bin/arch", "-x86_64", "/usr/bin/true"]),
            scratch,
            30,
            directory / "native-cpu-check.log",
            stop=stop,
            env=environment() | {"HOME": str(scratch), "TMPDIR": str(scratch)},
        )
    if rc:
        raise PipelineError(
            f"{target.name}: official ARM-labelled artifact contains x86_64 code, but Rosetta is unavailable. Install Rosetta once on this host or configure an ARM-compatible compiler version; no project source or version was changed."
        )


def native_init(worktree, command):
    try:
        return _native_init(worktree, command)
    except (ValueError, KeyError, TypeError, ET.ParseError) as error:
        raise PipelineError(
            "invalid native compiler verification evidence; prepare again"
        ) from error


def groovy_literal(value):
    # JSON double-quoted strings are executable GStrings in Groovy.
    return (
        "'"
        + str(value)
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        + "'"
    )


def _native_init(worktree, command):
    root = tool_root(worktree, command)
    rules = []
    for project, group, artifact, version in declarations(worktree):
        file = contained(
            worktree, str((root / f"{artifact}-{version}-{classifier()}.exe").relative_to(worktree))
        )
        marker = file.with_suffix(".json")
        if not file.is_file() or not marker.is_file() or marker.is_symlink():
            raise PipelineError("native compiler preparation required before execution")
        if file.stat().st_size > 128 * 2**20 or marker.stat().st_size > 2**20:
            raise PipelineError("native compiler evidence exceeds safety limit")
        data = file.read_bytes()
        metadata = json.loads(marker.read_text())
        if hashlib.sha256(data).hexdigest() != metadata["sha256"]:
            raise PipelineError("prepared native compiler checksum mismatch")
        pinned = project_checksum(worktree, group, artifact, version, file.name)
        if metadata.get("project_integrity") != pinned:
            raise PipelineError("native compiler verification policy changed; prepare again")
        from dev_orchestra.infrastructure.artifacts import checksum

        algorithm, expected = checksum(pinned or metadata["integrity"])
        if hashlib.new(algorithm, data).digest() != expected:
            raise PipelineError("prepared native compiler integrity mismatch")
        setting = (
            "protoc { locator -> locator.path = " + groovy_literal(file) + " }"
            if TOOLS[group, artifact] == "protoc"
            else "plugins { locators -> locators.getByName('grpc').path = "
            + groovy_literal(file)
            + " }"
        )
        rules.append(
            "if (project.projectDir.canonicalPath == "
            + groovy_literal((worktree / project).resolve())
            + ") { project.afterEvaluate { if (project.plugins.hasPlugin('com.google.protobuf')) { project.protobuf."
            + setting
            + " } } }"
        )
    return "\nallprojects { project ->\n" + "\n".join(rules) + "\n}\n" if rules else ""
