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


def check_stop(stop):
    if value := stop():
        raise StopRequested(value)


def seed_cache(source, target, stop):
    """Copy regular artifacts only; never share writable inodes or user settings."""
    if not source.is_dir() or source.is_symlink():
        return
    if source.resolve() == target.resolve():
        return
    if target.resolve().is_relative_to(source.resolve()) or source.resolve().is_relative_to(
        target.resolve()
    ):
        raise PipelineError("source and isolated Java caches must not overlap")
    count, size = 0, 0
    for parent, dirs, files in os.walk(source, followlinks=False):
        check_stop(stop)
        dirs[:] = [
            name
            for name in dirs
            if not name.startswith(".") and not (Path(parent) / name).is_symlink()
        ]
        for name in files:
            check_stop(stop)
            src = Path(parent) / name
            if (
                src.is_symlink()
                or not src.is_file()
                or name.startswith(".env")
                or name
                in {"settings.xml", "settings-security.xml", "gradle.properties", "gc.properties"}
                or src.suffix in {".lock", ".lck", ".key", ".pem", ".part", ".lastUpdated"}
            ):
                continue
            dst = target / src.relative_to(source)
            if any(p.is_symlink() for p in [dst, *dst.parents]):
                raise PipelineError("symlink Java cache paths are not allowed")
            if dst.exists():
                continue
            before = src.stat()
            count += 1
            size += before.st_size
            if count > 100000 or size > 4 * 2**30:
                raise PipelineError("Java cache seeding exceeds safety limit")
            dst.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".seed-", dir=dst.parent)
            temp = Path(name)
            try:
                with os.fdopen(fd, "wb") as output, src.open("rb") as input_file:
                    while chunk := input_file.read(2**20):
                        check_stop(stop)
                        output.write(chunk)
                after = src.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise PipelineError("host Java cache changed during copy; retry preparation")
                temp.chmod(before.st_mode & 0o755)
                temp.replace(dst)
            finally:
                temp.unlink(missing_ok=True)


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

    tracked = [name for name in Git(worktree).call("ls-files", "-z").split("\0") if name]
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
            if (
                tool == "gradle"
                and required
                and has_toolchain(read, command.working_directory)
                and required not in local_jdks().values()
            ):
                raise PipelineError(f"JDK toolchain {required} unavailable; install it and resume")
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
            elif not shutil.which(command.argv[0]):
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
            cache = outputs[0]
            source = host_cache(tool) / ("repository" if tool == "maven" else "caches/modules-2")
            seed_cache(source, cache if tool == "maven" else cache / "caches/modules-2", stop)
            if distribution:
                host_dists = host_cache(tool) / "wrapper/dists"
                # Maven's script-only Wrapper omits -bin in its cache directory name.
                names = (
                    {distribution, distribution.removesuffix("-bin")}
                    if tool == "maven"
                    else {distribution}
                )
                for name in names:
                    seed_cache(host_dists / name, cache / "wrapper/dists" / name, stop)
            check_artifacts(
                cache, lambda name: read(str(PurePosixPath(command.working_directory) / name)), tool
            )
            directory.mkdir(parents=True, exist_ok=True)
            if tool == "gradle":
                from dev_orchestra.infrastructure.java_native import prepare_native_tools

                prepare_native_tools(worktree, command, directory, stop)
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
                        [*runtime_argv[: 2 if runtime_argv[0] == "/bin/sh" else 1], "--version"],
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
        except StopRequested:
            raise
        except (PipelineError, OSError, ValueError, ET.ParseError) as error:
            raise PipelineError(
                sanitize(
                    f"Java environment unavailable for {key} (working directory: {command.working_directory}): {error}"
                )
            ) from error
