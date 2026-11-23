"""Discover local JDKs and seed isolated Java caches without running online builds."""

import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure.files import contained, relative_path
from dev_orchestra.infrastructure.gradle_preparation import (
    ModuleProgress,
    ReadinessRecord,
    readiness_binding,
)
from dev_orchestra.infrastructure.java_cache import (
    GradleSeeds,
    cache_identity,
    check_stop,
    fingerprint,
    seed_cache,
)
from dev_orchestra.infrastructure.process import environment, execute, sanitize
from dev_orchestra.infrastructure.test_runner import sandbox_command


def java_tool(command):
    name = Path(wrapper_argv(command.argv)[0]).name
    return (
        "maven" if name in {"mvn", "mvnw"} else "gradle" if name in {"gradle", "gradlew"} else None
    )


def wrapper_argv(argv):
    """Recognize a literal POSIX wrapper invocation, never arbitrary shell code."""
    if len(argv) > 1 and argv[0] == "/bin/sh" and Path(argv[1]).name in {"gradlew", "mvnw"}:
        return argv[1:]
    return argv


def java_argv(worktree, command):
    argv = list(command.argv)
    raw = wrapper_argv(argv)
    if Path(raw[0]).name not in {"gradlew", "mvnw"}:
        return argv
    cwd = worktree / command.working_directory
    path = Path(os.path.normpath(cwd / raw[0]))
    path = contained(worktree, path.relative_to(worktree).as_posix())
    if not path.is_file():
        raise PipelineError("Java Wrapper script missing")
    if raw is argv and not os.access(path, os.X_OK):
        return ["/bin/sh", *argv]
    return argv


def major(value):
    match = re.match(r"(?:1\.)?(\d+)", value.strip().replace("_", "."))
    return int(match[1]) if match else None


def required_jdk(read):
    """Read literal compiler/toolchain declarations; never evaluate build scripts."""
    pom = read("pom.xml")
    if pom:
        if "<!DOCTYPE" in pom or "<!ENTITY" in pom:
            raise PipelineError("XML declarations denied in Java configuration")
        try:
            tree = ET.fromstring(pom)
            for element in tree.iter():
                element.tag = element.tag.rsplit("}", 1)[-1]
            props = tree.find("properties")
            if props is not None:
                for name in (
                    "maven.compiler.release",
                    "java.version",
                    "maven.compiler.target",
                    "maven.compiler.source",
                ):
                    if value := props.findtext(name):
                        if re.fullmatch(r"(?:1\.)?\d+(?:\.\d+)*", value.strip()):
                            return major(value)
        except ET.ParseError:
            # Project syntax errors belong to the implementation/test stages.
            return None
    for name in ("build.gradle", "build.gradle.kts"):
        text = read(name) or ""
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"(?m)^\s*//.*$", "", text)
        patterns = [
            r"JavaLanguageVersion\.of\(\s*(\d+)\s*\)",
            r"jvmToolchain\(\s*(\d+)\s*\)",
            r"sourceCompatibility\s*=\s*JavaVersion\.VERSION_((?:1_)?\d+)",
            r"""sourceCompatibility\s*=\s*["']((?:1\.)?\d+)["']""",
        ]
        for pattern in patterns:
            if match := re.search(pattern, text):
                return major(match[1])
    return None


def local_jdks():
    candidates = []
    if value := os.environ.get("JAVA_HOME"):
        candidates.append(Path(value).expanduser())
    if binary := shutil.which("javac"):
        candidates.append(Path(binary).resolve().parent.parent)
    for root, pattern in [
        (Path.home() / "Library/Java/JavaVirtualMachines", "*/Contents/Home"),
        (Path("/Library/Java/JavaVirtualMachines"), "*/Contents/Home"),
        (Path("/usr/lib/jvm"), "*"),
        (Path.home() / ".sdkman/candidates/java", "*"),
        (Path("/opt/homebrew/opt"), "openjdk*/libexec/openjdk.jdk/Contents/Home"),
    ]:
        candidates.extend(sorted(root.glob(pattern)))
    found = {}
    for path in candidates:
        path = path.resolve()
        if not all(os.access(path / "bin" / name, os.X_OK) for name in ("java", "javac")):
            continue
        release = path / "release"
        if not release.is_file() or release.stat().st_size > 65536:
            continue
        match = re.search(r'^JAVA_VERSION="([^"]+)"', release.read_text(), re.M)
        if match and (version := major(match[1])):
            found[path] = version
    return found


def project_jdk(read, directory="."):
    path = PurePosixPath(directory)
    for parent in [path, *path.parents]:
        if version := required_jdk(lambda name: read(str(parent / name))):
            return version
    return None


def gradle_release(read, directory="."):
    path = PurePosixPath(directory)
    for parent in [path, *path.parents]:
        text = read(str(parent / "gradle/wrapper/gradle-wrapper.properties")) or ""
        if match := re.search(r"gradle-(\d+)\.(\d+)(?:\.(\d+))?-(?:bin|all)\.zip", text):
            return tuple(int(v or 0) for v in match.groups())
    return None


def gradle_jvm_supported(version, jdk):
    # Official Gradle Java runtime compatibility matrix; toolchain support differs.
    minimum = {
        8: (2, 0),
        9: (4, 3),
        10: (4, 7),
        11: (5, 0),
        12: (5, 4),
        13: (6, 0),
        14: (6, 3),
        15: (6, 7),
        16: (7, 0),
        17: (7, 3),
        18: (7, 5),
        19: (7, 6),
        20: (8, 3),
        21: (8, 5),
        22: (8, 8),
        23: (8, 10),
        24: (8, 14),
        25: (9, 1),
        26: (9, 4),
        27: (9, 8),
    }
    return version is None or (
        version[:2] >= minimum.get(jdk, (0, 0)) and not (version[0] >= 9 and jdk < 17)
    )


def has_toolchain(read, directory="."):
    path = PurePosixPath(directory)
    for parent in [path, *path.parents]:
        for name in ("build.gradle", "build.gradle.kts"):
            text = re.sub(r"/\*.*?\*/", "", read(str(parent / name)) or "", flags=re.S)
            text = re.sub(r"(?m)^\s*//.*$", "", text)
            if re.search(r"JavaLanguageVersion\.of\(|jvmToolchain\(", text):
                return True
    return False


def choose_project_jdk(read, directory=".", tool="maven"):
    required = project_jdk(read, directory)
    version = gradle_release(read, directory) if tool == "gradle" else None
    return select_jdk(required, version, tool == "gradle" and has_toolchain(read, directory))


def select_jdk(required=None, gradle=None, toolchain=False):
    choices = local_jdks()
    if toolchain and required and required not in choices.values():
        return None
    matches = [
        (path, version)
        for path, version in choices.items()
        if (required is None or version >= required) and gradle_jvm_supported(gradle, version)
    ]
    if not matches and toolchain and required and any(v >= required for v in choices.values()):
        matches = [(p, v) for p, v in choices.items() if gradle_jvm_supported(gradle, v)]
    if not matches:
        return None
    exact = [item for item in matches if item[1] == required]
    return str((exact[0] if exact else max(matches, key=lambda item: item[1]))[0])


def option(argv, flags, prefix=None):
    for i, token in enumerate(argv):
        if prefix and token.startswith(prefix):
            return token[len(prefix) :]
        if token in flags:
            if i + 1 == len(argv):
                raise PipelineError(f"missing value for {token}")
            return argv[i + 1]
        for flag in flags:
            if token.startswith(flag + "="):
                return token[len(flag) + 1 :]
    return None


def cache_paths(command):
    if java_tool(command) == "maven":
        value = option(command.argv, (), "-Dmaven.repo.local=")
        if not value:
            raise PipelineError("Maven requires an isolated -Dmaven.repo.local; create a new Run")
        return [relative_path(value)]
    home = option(command.argv, ("--gradle-user-home", "-g"))
    project = option(command.argv, ("--project-cache-dir",))
    if not home or not project:
        raise PipelineError("Gradle requires isolated user/project caches; create a new Run")
    return [relative_path(home), relative_path(project)]


def java_environment(worktree, command):
    tool = java_tool(command)
    if not tool:
        return {}
    cwd = (
        worktree
        if command.working_directory == "."
        else contained(worktree, command.working_directory)
    )
    home = contained(worktree, (cwd / cache_paths(command)[0]).relative_to(worktree).as_posix())
    env = {"MAVEN_USER_HOME" if tool == "maven" else "GRADLE_USER_HOME": str(home)}
    if "JAVA_HOME" in command.env:
        env["JAVA_HOME"] = str(Path(command.env["JAVA_HOME"]).expanduser())
    else:

        def read(name):
            path = contained(worktree, name)
            return path.read_text() if path.is_file() and path.stat().st_size <= 2**20 else None

        if jdk := choose_project_jdk(read, command.working_directory, tool):
            env["JAVA_HOME"] = jdk
    if tool == "gradle":
        env["ORG_GRADLE_PROJECT_org.gradle.java.installations.paths"] = ",".join(
            str(p) for p in local_jdks()
        )
        env["ORG_GRADLE_PROJECT_org.gradle.java.installations.auto-download"] = "false"
    return env


def host_cache(tool):
    value = os.environ.get("MAVEN_USER_HOME" if tool == "maven" else "GRADLE_USER_HOME")
    return (
        Path(value).expanduser()
        if value
        else Path.home() / (".m2" if tool == "maven" else ".gradle")
    )


def wrapper_distribution(cwd, tool):
    name = (
        ".mvn/wrapper/maven-wrapper.properties"
        if tool == "maven"
        else "gradle/wrapper/gradle-wrapper.properties"
    )
    path = contained(cwd, name)
    if not path.is_file() or path.stat().st_size > 65536:
        raise PipelineError(f"Wrapper properties missing or oversized: {name}")
    text = path.read_text()
    match = re.search(r"^distributionUrl\s*[=:]\s*(.+)$", text, re.M)
    if not match:
        raise PipelineError("Wrapper distributionUrl missing")
    url = match[1].strip().replace("\\:", ":")
    # Only select a local cache directory; this URL is never downloaded by the host.
    basename = Path(urlsplit(url).path).name
    if not re.fullmatch(
        r"(?:gradle-[0-9][0-9A-Za-z.+-]*-(?:bin|all)|apache-maven-[0-9][0-9A-Za-z.+-]*-bin)\.zip",
        basename,
    ):
        # Custom layouts may already be prepared manually; do not prevent offline use.
        return None
    for key in ("distributionBase", "zipStoreBase"):
        base = re.search(r"^" + key + r"\s*[=:]\s*(.+)$", text, re.M)
        if base and base[1].strip() != (
            "MAVEN_USER_HOME" if tool == "maven" else "GRADLE_USER_HOME"
        ):
            raise PipelineError("Wrapper cache base must use its isolated user home")
    return basename.removesuffix(".zip")


def declared_artifacts(read, tool):
    """Check only literal, directly requested coordinates; no guessed BOM versions."""
    if tool == "gradle":
        text = (read("build.gradle") or "") + "\n" + (read("build.gradle.kts") or "")
        text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
        text = re.sub(r"(?m)^\s*//.*$", "", text)
        pattern = r"""(?:implementation|api|compileOnly|runtimeOnly|testImplementation|testRuntimeOnly|annotationProcessor|classpath)\s*\(?\s*["']([\w.-]+):([\w.-]+):([\w.+-]+)["']"""
        return [(g, a, v, None) for g, a, v in re.findall(pattern, text) if "+" not in v]
    text = read("pom.xml")
    if not text:
        return []
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise PipelineError("XML declarations denied in Java configuration")
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []
    for node in root.iter():
        node.tag = node.tag.rsplit("}", 1)[-1]
    props = {node.tag: node.text or "" for node in root.findall("properties/*")}

    def value(node, name):
        raw = (node.findtext(name) or "").strip()
        return props.get(raw[2:-1], "") if raw.startswith("${") and raw.endswith("}") else raw

    result = []
    for node in root.findall("parent") + root.findall("dependencies/dependency"):
        if (
            node.tag == "parent"
            and (parent := node.findtext("relativePath", "../pom.xml"))
            and read(parent)
        ):
            continue
        if node.findtext("scope") == "system":
            continue
        g, a, v = (value(node, name) for name in ("groupId", "artifactId", "version"))
        if not all(re.fullmatch(r"[\w][\w.+-]*", part) for part in (g, a, v)):
            continue
        kind = "pom" if node.tag == "parent" else (node.findtext("type") or "jar")
        if kind not in {"pom", "jar", "war", "zip", "aar", "test-jar"}:
            continue
        classifier = node.findtext("classifier") or ("tests" if kind == "test-jar" else "")
        if classifier and not re.fullmatch(r"[\w][\w.-]*", classifier):
            continue
        suffix = (
            ("-" + classifier if classifier else "") + "." + ("jar" if kind == "test-jar" else kind)
        )
        result.append((g, a, v, suffix))
    return result


def check_artifacts(cache, read, tool):
    missing = []
    for group, artifact, version, kind in declared_artifacts(read, tool):
        if tool == "maven":
            path = cache / group.replace(".", "/") / artifact / version
            ready = (path / f"{artifact}-{version}{kind}").is_file()
        else:
            path = cache / "caches/modules-2/files-2.1" / group / artifact / version
            ready = path.is_dir() and any(
                p.is_file() and not p.is_symlink() for p in path.glob("*/*")
            )
        if not ready:
            missing.append(f"{group}:{artifact}:{version}")
    if missing:
        raise PipelineError(
            "offline dependency cache missing "
            + ", ".join(missing[:8])
            + "; populate the host dependency cache or this Run's isolated cache and resume"
        )


def prepare_java(worktree, config, directory, stop=lambda: None):
    commands = [(key, c) for key, c in config.commands.items() if java_tool(c)]
    if not commands:
        return
    worktree = worktree.resolve()
    from dev_orchestra.infrastructure.git import Git
    from dev_orchestra.infrastructure.java_native import declarations, prepare_native_tools

    git = Git(worktree, config.output_paths)
    tracked = [name for name in git.call("ls-files", "-z").split("\0") if name]
    gradle_dirs = [c.working_directory for _, c in commands if java_tool(c) == "gradle"]
    progress = ModuleProgress(gradle_dirs)
    seeds = GradleSeeds(directory)
    clean_sha = None
    if gradle_dirs and not git.dirty() and not git.call("diff", "--name-only", "HEAD"):
        clean_sha = git.sha()
    policy = fingerprint(config.model_dump(mode="json")) if gradle_dirs else None
    prepared_gradle = set()
    tool_stamps = {}
    native_entries = None
    for key, command in commands:
        check_stop(stop)
        try:
            tool = java_tool(command)
            cwd = (
                worktree
                if command.working_directory == "."
                else contained(worktree, command.working_directory)
            )

            def read(name):
                name = os.path.normpath(name)
                if name == ".." or name.startswith("../"):
                    return None
                path = contained(worktree, name)
                if path.is_file() and path.stat().st_size <= 2**20:
                    return path.read_text()
                return None

            required = project_jdk(read, command.working_directory)
            toolchains = []
            if tool == "gradle" and required and has_toolchain(read, command.working_directory):
                toolchains = [path for path, version in local_jdks().items() if version == required]
                if not toolchains:
                    raise PipelineError(
                        f"JDK toolchain {required} unavailable; install it and resume"
                    )
            jdk = command.env.get("JAVA_HOME") or choose_project_jdk(
                read, command.working_directory, tool
            )
            if not jdk:
                raise PipelineError(
                    f"JDK {required or '(java + javac)'} unavailable; install a matching JDK or configure JAVA_HOME and resume"
                )
            home = Path(jdk).expanduser()
            if not home.is_absolute():
                raise PipelineError("JAVA_HOME must be an absolute JDK path")
            if not all(os.access(home / "bin" / name, os.X_OK) for name in ("java", "javac")):
                raise PipelineError(
                    "JAVA_HOME must point to a JDK containing executable java and javac"
                )
            if tool == "maven" and not any(arg in {"-o", "--offline"} for arg in command.argv):
                raise PipelineError(
                    "Maven requires offline mode; update project validation and create a new Run"
                )
            if tool == "gradle" and "--offline" not in command.argv:
                raise PipelineError(
                    "Gradle requires offline mode; update project validation and create a new Run"
                )
            runtime_argv = java_argv(worktree, command)
            executable = Path(wrapper_argv(runtime_argv)[0])
            distribution = None
            if executable.name in {"mvnw", "gradlew"}:
                executable = Path(os.path.normpath(cwd / executable))
                executable = contained(worktree, executable.relative_to(worktree).as_posix())
                distribution = wrapper_distribution(executable.parent, tool)
            elif binary := shutil.which(command.argv[0]):
                executable = Path(binary)
            else:
                raise PipelineError(
                    f"{command.argv[0]} not installed; use the project's Wrapper or install the build tool"
                )
            outputs = []
            for value in cache_paths(command):
                relative_path(value)
                path = contained(worktree, (cwd / value).relative_to(worktree).as_posix())
                if not any(
                    path.is_relative_to(contained(worktree, p)) for p in config.output_paths
                ):
                    raise PipelineError(
                        f"Java cache {value} is not an allowed output; create a new Run"
                    )
                if any((worktree / name).is_relative_to(path) for name in tracked):
                    raise PipelineError("Java cache output contains tracked source")
                path.mkdir(parents=True, exist_ok=True)
                outputs.append(path)
            launcher = runtime_argv[: 2 if runtime_argv[0] == "/bin/sh" else 1]
            if tool == "gradle":
                # Validate each command before consulting either level of readiness records.
                gradle_environment = (
                    cwd,
                    tuple(launcher),
                    distribution,
                    tuple(outputs),
                    tuple(sorted((command.env | {"JAVA_HOME": str(home)}).items())),
                    command.network,
                )
                if gradle_environment in prepared_gradle:
                    # Native integrity and CPU eligibility still depend on the files and task.
                    prepare_native_tools(
                        worktree,
                        command,
                        directory,
                        stop,
                        entries=native_entries,
                    )
                    progress.complete(command.working_directory)
                    continue
                binding = None
                if clean_sha:
                    tools = [home, *toolchains]
                    if executable.name != "gradlew":
                        lib = executable.resolve().parent.parent / "lib"
                        # A custom launcher may delegate to an installation we cannot identify.
                        if any(lib.glob("gradle-launcher-*.jar")):
                            tools.append(lib)
                        else:
                            tools = []
                    for path in tools:
                        if path not in tool_stamps:
                            tool_stamps[path] = cache_identity(
                                [path.resolve()], stop, toolchain=True
                            )
                    if tools and all(tool_stamps[path] for path in tools):
                        binding = readiness_binding(
                            worktree,
                            cwd,
                            executable,
                            clean_sha,
                            policy,
                            [tool_stamps[path] for path in tools],
                            java_environment(worktree, command),
                            required,
                        )
                record = ReadinessRecord(directory, gradle_environment, binding)
                reused = record.matches(outputs, stop)
                if not reused:
                    seeds.populate(
                        worktree,
                        cwd,
                        executable,
                        home,
                        distribution,
                        host_cache(tool),
                        outputs[0],
                        stop,
                    )
            else:
                cache = outputs[0]
                seed_cache(host_cache(tool) / "repository", cache, stop)
                if distribution:
                    # Maven's script-only Wrapper omits -bin in its cache directory name.
                    for name in {distribution, distribution.removesuffix("-bin")}:
                        subpath = Path("wrapper/dists") / name
                        seed_cache(host_cache(tool) / subpath, cache / subpath, stop)
            check_artifacts(
                outputs[0],
                lambda name: read(str(PurePosixPath(command.working_directory) / name)),
                tool,
            )
            directory.mkdir(parents=True, exist_ok=True)
            if tool == "gradle":
                if native_entries is None:
                    native_entries = declarations(worktree)
                prepare_native_tools(worktree, command, directory, stop, entries=native_entries)
                if reused:
                    prepared_gradle.add(gradle_environment)
                    progress.complete(command.working_directory)
                    continue
            with tempfile.TemporaryDirectory(prefix="devorchestra-java-") as temp:
                scratch = Path(temp).resolve()
                env = environment() | {
                    "HOME": str(scratch),
                    "TMPDIR": str(scratch),
                    "TMP": str(scratch),
                    "TEMP": str(scratch),
                    "JAVA_HOME": str(home),
                    "JAVA_TOOL_OPTIONS": f'-Duser.home="{scratch}" -Djava.io.tmpdir="{scratch}"',
                }
                env.update(java_environment(worktree, command))
                rc, output = execute(
                    sandbox_command(
                        worktree, outputs, scratch, [str(home / "bin/javac"), "-version"]
                    ),
                    cwd,
                    30,
                    directory / f"{key}-jdk.log",
                    stop=stop,
                    env=env,
                )
                match = re.search(r"javac\s+(\S+)", output)
                actual = major(match[1]) if match else None
                if (
                    rc
                    or not actual
                    or (
                        required
                        and actual < required
                        and not (
                            tool == "gradle" and has_toolchain(read, command.working_directory)
                        )
                    )
                ):
                    raise PipelineError(
                        f"JDK version check failed (required {required or 'a JDK'}): {output[-800:]}"
                    )
                if tool == "gradle" and not gradle_jvm_supported(
                    gradle_release(read, command.working_directory), actual
                ):
                    raise PipelineError(
                        "JAVA_HOME JDK is incompatible with the Gradle Wrapper version"
                    )
                rc, output = execute(
                    sandbox_command(
                        worktree,
                        outputs,
                        scratch,
                        [*launcher, "--version"],
                        command.network,
                    ),
                    cwd,
                    90,
                    directory / f"{key}-tool.log",
                    stop=stop,
                    env=env,
                )
                if rc:
                    raise PipelineError(
                        f"offline {tool} bootstrap failed: {output[-1200:]}; prepare its distribution in the local host cache or this Run's isolated cache and resume"
                    )
            if tool == "gradle":
                record.save(outputs, stop)
                prepared_gradle.add(gradle_environment)
                progress.complete(command.working_directory, checked=True)
        except StopRequested:
            raise
        except (PipelineError, OSError, ValueError, ET.ParseError) as error:
            raise PipelineError(
                sanitize(
                    f"Java environment unavailable for {key} (working directory: {command.working_directory}): {error}"
                )
            ) from error
