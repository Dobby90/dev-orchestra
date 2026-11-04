"""Read-only tooling discovery from a validated repository's committed base tree."""

import json
import platform
import re
import shlex
from pathlib import Path, PurePosixPath

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.web_contracts import ValidationCandidate, ValidationDetection
from dev_orchestra.config import TestCommand
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.dependencies import vite_cache_output
from dev_orchestra.infrastructure.files import relative_path
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.playwright import browser_cache


def script_tokens(value):
    """Only analyze a single literal command; never interpret shell expressions."""
    if re.search(r"[;&|<>`$\n\r]", value):
        return []
    try:
        return shlex.split(value)
    except ValueError:
        return []


def flag_values(tokens, flag):
    values = []
    for index, token in enumerate(tokens):
        if token == flag:
            values.append(tokens[index + 1] if index + 1 < len(tokens) else "")
        elif token.startswith(flag + "="):
            values.append(token.partition("=")[2])
    return values


def node_junit_report(tokens):
    # Node stops option parsing at the first test file (or --). Reporter-looking
    # arguments after that point are filenames, not report configuration.
    value_flags = {
        "--test-reporter",
        "--test-reporter-destination",
        "--import",
        "--require",
        "-r",
        "--loader",
        "--experimental-loader",
        "--test-name-pattern",
        "--test-skip-pattern",
        "--test-concurrency",
        "--test-timeout",
        "--conditions",
        "-C",
        "--inspect-port",
    }
    index = 1 if tokens and Path(tokens[0]).name == "node" else 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--" or not token.startswith("-"):
            tokens = tokens[:index]
            break
        index += 2 if token in value_flags else 1
    reporters = flag_values(tokens, "--test-reporter")
    destinations = flag_values(tokens, "--test-reporter-destination")
    if len(reporters) != len(destinations) or reporters.count("junit") != 1:
        return None
    destination = destinations[reporters.index("junit")]
    if destination in {"stdout", "stderr"} or not destination:
        return None
    try:
        path = relative_path(destination)
    except PipelineError:
        return None
    if any(c in path for c in "*?[") or any(
        part.startswith(".") for part in PurePosixPath(path).parts
    ):
        return None
    return path


def npm_node_options(report):
    return (
        "--node-options=--test-reporter=spec --test-reporter-destination=stdout "
        "--test-reporter=junit --test-reporter-destination=" + report
    )


def configure_node_test(item, tokens, manager, script, scripts, prefix, files):
    """Use npm's lifecycle option, not trailing Node flags interpreted as test files."""
    report = node_junit_report(tokens)
    if report:
        item.command.junit_report = prefix + report
        output = str(PurePosixPath(prefix + report).parent)
        if output != "." and not any(p == output or p.startswith(output + "/") for p in files):
            item.output_paths = list(dict.fromkeys([*item.output_paths, output]))
            item.warnings = []
        else:
            item.warnings = [
                "설정된 JUnit 경로의 상위 디렉터리에 추적 소스가 없어야 합니다. 별도 출력 디렉터리를 지정하세요."
            ]
        return
    local_report = item.command.junit_report.removeprefix(prefix)
    # Existing reporter settings, npm configuration and test lifecycle hooks may
    # override NODE_OPTIONS. Do not guess or replace their behavior.
    ancestors = PurePosixPath(prefix or ".").parents
    npmrc_paths = {prefix + ".npmrc", ".npmrc", *(str(p / ".npmrc") for p in ancestors)}
    hooks = [scripts.get("pre" + script), scripts.get("post" + script)]
    if (
        manager == "npm"
        and not any(t.startswith(("--test-reporter", "--watch")) for t in tokens)
        and not any(n in files for n in npmrc_paths)
        and not any(hooks)
    ):
        item.command.argv = [manager, npm_node_options(local_report), "run", script]
        item.warnings = []
        return
    item.warnings = [
        f"Node 내장 테스트의 JUnit 파일 출력을 자동 확정할 수 없습니다. {script} 스크립트에서 "
        f"테스트 파일 경로 앞에 --test-reporter=junit --test-reporter-destination={local_report}를 "
        f"설정하고 수집 경로 {item.command.junit_report}와 일치시키세요. 기존 reporter·생명주기 훅·패키지 관리자 설정은 유지합니다."
    ]


def candidate(tool, directory=".", *, argv=None, evidence=(), label=None):
    prefix = "" if directory == "." else directory + "/"
    command = {"working_directory": directory, "timeout_seconds": 600}
    warnings = []
    outputs = [prefix + "build"]
    writes = [prefix + "tests"]
    if tool == "pytest":
        command.update(
            argv=argv or ["python", "-m", "pytest", "--junitxml=build/junit.xml", "-q"],
            junit_report=prefix + "build/junit.xml",
        )
        outputs.append(prefix + ".pytest_cache")
        warnings.append("Python 실행 파일은 대상 프로젝트의 pytest 환경으로 지정하세요.")
    elif tool == "node-test":
        command.update(argv=argv or ["npm", "test"], junit_report=prefix + "build/unit.xml")
        warnings.append(
            "테스트 스크립트가 단발 실행하고 build/unit.xml에 JUnit을 생성하도록 설정하세요."
        )
    elif tool == "playwright":
        command.update(
            argv=argv or ["npm", "run", "test:browser", "--", "--reporter=junit"],
            junit_report=prefix + "build/browser.xml",
            env={
                "PLAYWRIGHT_JUNIT_OUTPUT_FILE": "build/browser.xml",
                "PLAYWRIGHT_BROWSERS_PATH": str(browser_cache()),
            },
            network="loopback",
        )
        outputs += [prefix + "test-results", prefix + "playwright-report"]
        writes = [prefix + "tests", prefix + "e2e"]
        warnings.append(
            "기본 캐시의 Playwright 브라우저는 실행 전에 버전에 맞춰 자동 준비합니다. "
            "최초 준비에는 외부 네트워크가 필요하며 서버는 127.0.0.1을 사용해야 합니다."
        )
        if platform.system() != "Darwin":
            warnings.append(
                "현재 loopback 브라우저 샌드박스는 macOS에서 지원합니다. 이 환경에서는 실행을 차단합니다."
            )
    elif tool == "build":
        command.update(kind="check", argv=argv or ["npm", "run", "build"])
        outputs += [prefix + "dist"]
        writes = []
    elif tool in {"maven", "maven-build"}:
        command.update(
            argv=argv or ["mvn", "-o", "-Dmaven.repo.local=.devorchestra-cache/maven", "test"],
            junit_report=prefix + "target/surefire-reports/TEST-*.xml",
        )
        outputs = [prefix + "target", prefix + ".devorchestra-cache/maven"]
        writes = [prefix + "src/test"]
        warnings.append(
            "JDK·Maven과 오프라인 의존성 캐시(.devorchestra-cache/maven)를 Worktree에 미리 준비하세요."
        )
    elif tool in {"gradle", "gradle-build"}:
        command.update(
            network="loopback",
            argv=argv
            or [
                "gradle",
                "--offline",
                "--no-daemon",
                "--gradle-user-home",
                ".devorchestra-cache/gradle",
                "--project-cache-dir",
                ".devorchestra-cache/project",
                "test",
                "--rerun-tasks",
            ],
            junit_report=prefix + "build/test-results/test/TEST-*.xml",
        )
        outputs += [prefix + ".devorchestra-cache"]
        writes = [prefix + "src/test"]
        warnings.append(
            "JDK·Gradle 배포본과 의존성 캐시(.devorchestra-cache/gradle)를 Worktree에 미리 준비하세요."
        )
        warnings.append(
            "Gradle 내부 잠금·프로세스 통신에는 loopback 허용이 필요합니다. 현재 macOS에서 지원합니다."
        )
    else:
        raise ValueError("unknown validation profile")
    if tool.endswith("-build"):
        command.update(kind="check", junit_report=None)
        command["argv"] = (
            ["package" if arg == "test" else arg for arg in command["argv"]]
            if tool == "maven-build"
            else ["assemble" if arg == "test" else arg for arg in command["argv"]]
        )
        if tool == "maven-build":
            command["argv"].append("-DskipTests")
    return ValidationCandidate(
        id=tool,
        label=label or tool,
        evidence=list(evidence),
        warnings=warnings,
        command=TestCommand.model_validate(command),
        write_paths=writes,
        output_paths=outputs,
    )


def profiles():
    labels = {
        "pytest": "Python · pytest",
        "node-test": "Node · 단위 테스트",
        "playwright": "Node · Playwright",
        "build": "Node · 빌드",
        "maven": "Java · Maven 테스트",
        "gradle": "Java · Gradle 테스트",
        "maven-build": "Java · Maven 빌드",
        "gradle-build": "Java · Gradle 빌드",
    }
    return [candidate(key, label=label) for key, label in labels.items()]


def detect_validation(info):
    git = Git(info.repository_path)
    # Git blobs avoid following repository symlinks and ignore uncommitted source changes.
    entries = git.call("ls-tree", "-rlz", info.base_sha).split("\0")
    if len(entries) > 20000:
        raise ApplicationError(
            "VALIDATION_ERROR", "설정 감지 대상은 추적 파일 20,000개 이하로 제한합니다."
        )
    files = {}
    for entry in entries:
        if not entry:
            continue
        metadata, path = entry.split("\t", 1)
        mode, kind, oid, size = metadata.split()
        if kind == "blob" and mode != "120000":
            files[path] = (oid, int(size))
    names = {
        "package.json",
        "pyproject.toml",
        "pytest.ini",
        "setup.cfg",
        "tox.ini",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
    }
    manifests = sorted(p for p in files if PurePosixPath(p).name in names)
    if len(manifests) > 128:
        raise ApplicationError(
            "VALIDATION_ERROR", "설정 파일이 128개를 초과합니다. 작업 대상을 좁혀 주세요."
        )
    found, warnings, python_dirs = [], [], set()

    def add(tool, path, **kwargs):
        if len(found) >= 128:
            raise ApplicationError(
                "VALIDATION_ERROR", "검증 후보가 128개를 초과합니다. 작업 대상을 좁혀 주세요."
            )
        directory = str(PurePosixPath(path).parent)
        item = candidate(tool, directory, evidence=[path], **kwargs)
        item.id = re.sub(r"[^A-Za-z0-9_-]", "_", f"{tool}_{len(found) + 1}")
        found.append(item)
        return item

    for path in manifests:
        relative_path(path)
        oid, size = files[path]
        if size > 262144:
            warnings.append(f"{path}: 설정 파일 크기 제한으로 감지하지 않았습니다.")
            continue
        text = git.call("cat-file", "blob", oid)
        name = PurePosixPath(path).name
        directory = str(PurePosixPath(path).parent)
        if name == "package.json":
            try:
                package = json.loads(text)
                scripts = package.get("scripts", {})
                if not isinstance(scripts, dict):
                    raise ValueError()
                declared = package.get("packageManager", "")
                pm = declared.split("@", 1)[0] if isinstance(declared, str) else ""
                prefix = "" if directory == "." else directory + "/"
                if pm not in {"npm", "pnpm", "yarn"}:
                    pm = next(
                        (
                            manager
                            for lock, manager in [
                                ("pnpm-lock.yaml", "pnpm"),
                                ("yarn.lock", "yarn"),
                                ("package-lock.json", "npm"),
                            ]
                            if prefix + lock in files
                        ),
                        "npm",
                    )
                dependencies = package.get("devDependencies", {}) | package.get("dependencies", {})
                for script, value in scripts.items():
                    if not isinstance(value, str) or not isinstance(script, str):
                        continue
                    if script == "build" or script in {"typecheck", "lint"}:
                        item = add(
                            "build", path, argv=[pm, "run", script], label=f"{directory} · {script}"
                        )
                        vite_cache = vite_cache_output(package, prefix, files, script)
                        if vite_cache and not any(
                            p.startswith(vite_cache + "/") or p == vite_cache for p in files
                        ):
                            item.output_paths.append(vite_cache)
                    elif script == "test" or script.startswith(("test:", "test-")):
                        argv = [pm, "run", script]
                        separator = ["--"] if pm == "npm" else []
                        tokens = script_tokens(value)
                        if tokens and tokens[0] == "playwright":
                            item = add(
                                "playwright",
                                path,
                                argv=argv + separator + ["--reporter=junit"],
                                label=f"{directory} · {script} (Playwright)",
                            )
                            report = "build/" + re.sub(r"[^A-Za-z0-9_-]", "_", script) + ".xml"
                            item.command.junit_report = prefix + report
                            item.command.env["PLAYWRIGHT_JUNIT_OUTPUT_FILE"] = report
                        else:
                            item = add(
                                "node-test", path, argv=argv, label=f"{directory} · {script}"
                            )
                            # Only append runner-specific flags when the script invokes that runner.
                            if tokens and tokens[0] == "vitest" and "vitest" in dependencies:
                                item.command.argv += separator + [
                                    "--run",
                                    "--reporter=junit",
                                    "--outputFile=build/unit.xml",
                                ]
                                item.warnings = []
                            safe_script = re.sub(r"[^A-Za-z0-9_-]", "_", script)
                            report = f"build/{safe_script}.xml"
                            item.command.junit_report = prefix + report
                            item.command.argv = [
                                arg.replace("build/unit.xml", report) for arg in item.command.argv
                            ]
                            item.warnings = [
                                w.replace("build/unit.xml", report) for w in item.warnings
                            ]
                            if tokens and tokens[0] == "node" and "--test" in tokens:
                                configure_node_test(
                                    item, tokens, pm, script, scripts, prefix, files
                                )
            except (ValueError, TypeError, AttributeError):
                warnings.append(f"{path}: package.json 형식을 확인해 주세요.")
        elif name == "pom.xml":
            argv = [
                "./mvnw"
                if ("" if directory == "." else directory + "/") + "mvnw" in files
                else "mvn"
            ]
            item = add(
                "maven",
                path,
                argv=argv + ["-o", "-Dmaven.repo.local=.devorchestra-cache/maven", "test"],
                label=f"{directory} · Maven 테스트",
            )
            add(
                "maven-build",
                path,
                argv=argv + ["-o", "-Dmaven.repo.local=.devorchestra-cache/maven", "test"],
                label=f"{directory} · Maven 빌드",
            )
        elif name.startswith("build.gradle"):
            prefix = "" if directory == "." else directory + "/"
            item = add("gradle", path, label=f"{directory} · Gradle 테스트")
            if prefix + "gradlew" in files:
                item.command.argv[0] = "./gradlew"
            add(
                "gradle-build",
                path,
                argv=item.command.argv.copy(),
                label=f"{directory} · Gradle 빌드",
            )
        elif name == "pytest.ini" or "pytest" in text:
            if directory not in python_dirs:
                item = add("pytest", path, label=f"{directory} · pytest")
                for parent in [PurePosixPath(directory), PurePosixPath(".")]:
                    interpreter = info.repository_path / parent / ".venv/bin/python"
                    if interpreter.is_file():
                        item.command.argv[0] = str(interpreter)
                        item.warnings = []
                        break
                python_dirs.add(directory)
    if not python_dirs:
        tests = [p for p in files if re.search(r"(?:^|/)(?:test_[^/]+|[^/]+_test)\.py$", p)]
        if tests:
            item = candidate("pytest", evidence=tests[:5], label=". · pytest")
            item.id = "pytest_discovered"
            found.append(item)
    # Aggregate root Java builds can write per-module results. Authorize only known
    # module output directories; every glob match is checked against these at runtime.
    for item in found:
        prefix = (
            "" if item.command.working_directory == "." else item.command.working_directory + "/"
        )
        if item.command.kind == "test":
            for path in files:
                if (
                    path.startswith(prefix)
                    and re.search(
                        r"(?:^|/)(?:test_[^/]+\.py|[^/]+_test\.py|[^/]+\.(?:test|spec)\.[cm]?[jt]sx?)$",
                        path,
                    )
                    and path not in item.write_paths
                ):
                    item.write_paths.append(path)
        tool = item.id.split("_", 1)[0]
        if tool not in {"maven", "maven-build", "gradle", "gradle-build"}:
            continue
        is_maven = tool.startswith("maven")
        nested = [
            p
            for p in manifests
            if p.startswith(prefix)
            and PurePosixPath(p).name
            in ({"pom.xml"} if is_maven else {"build.gradle", "build.gradle.kts"})
        ]
        if len(nested) > 1:
            for path in nested:
                parent = PurePosixPath(path).parent
                output = str(parent / ("target" if is_maven else "build"))
                if output not in item.output_paths:
                    item.output_paths.append(output)
                tests = str(parent / "src/test")
                if tests not in item.write_paths and item.command.kind == "test":
                    item.write_paths.append(tests)
            if item.command.kind == "test":
                suffix = (
                    "target/surefire-reports/TEST-*.xml"
                    if is_maven
                    else "build/test-results/test/TEST-*.xml"
                )
                item.command.junit_report = prefix + "**/" + suffix
                item.warnings.append(
                    "멀티모듈 보고서를 함께 수집합니다. 중복된 하위 모듈 후보는 선택 해제할 수 있습니다."
                )
    if not found:
        warnings.append(
            "지원하는 검증 설정을 찾지 못했습니다. 도구를 직접 선택하고 명령을 확인하세요."
        )
    if any(c.command.kind == "test" and c.command.working_directory != "." for c in found):
        warnings.append(
            "하위 프로젝트별로 감지했습니다. 모듈 간 의존성이 있으면 실행 명령과 보고서 경로를 확인하세요."
        )
    return ValidationDetection(base_sha=info.base_sha, candidates=found, warnings=warnings)


def script_identity(argv):
    if not argv or Path(argv[0]).name not in {"npm", "pnpm", "yarn"}:
        return None
    manager = Path(argv[0]).name
    tail = argv[1:]
    if tail == ["test"]:
        return manager, "test"
    if tail and tail[0].startswith("--node-options="):
        tail = tail[1:]
    if len(tail) >= 2 and tail[0] == "run":
        return manager, tail[1]
    return None


def complete_run_validation(config, info):
    """Complete derived outputs before freezing a new Run; never edit saved policies."""
    snapshot = config.model_copy(deep=True)
    selected = [c for c in snapshot.tests.commands.values() if script_identity(c.argv)]
    if not selected:
        return snapshot
    detected = detect_validation(info)
    for command in selected:
        for item in detected.candidates:
            if command.working_directory != item.command.working_directory or script_identity(
                command.argv
            ) != script_identity(item.command.argv):
                continue
            for output in item.output_paths:
                if output.endswith("node_modules/.vite-temp") and not any(
                    PurePosixPath(output).is_relative_to(PurePosixPath(p))
                    for p in snapshot.tests.output_paths
                ):
                    snapshot.tests.output_paths.append(output)
    return snapshot


def report_warning(command, candidates):
    """Return only a statically known mismatch, or an explicit uncertainty notice."""
    prefix = "" if command.working_directory == "." else command.working_directory + "/"
    report = None
    known = False
    if Path(command.argv[0]).name == "node" and "--test" in command.argv:
        report = node_junit_report(command.argv)
        known = True
    else:
        identity = script_identity(command.argv)
        if not identity:
            return None
        for item in candidates:
            if (
                item.command.kind != "test"
                or item.command.working_directory != command.working_directory
                or script_identity(item.command.argv) != identity
            ):
                continue
            if item.id.startswith("node-test") and item.warnings:
                return " ".join(item.warnings)
            node_options = flag_values(item.command.argv, "--node-options")
            if node_options:
                saved = flag_values(command.argv, "--node-options")
                report = node_junit_report(script_tokens(saved[0])) if len(saved) == 1 else None
            elif item.id.startswith("playwright"):
                if "junit" not in flag_values(command.argv, "--reporter"):
                    return "Playwright의 JUnit reporter와 파일 출력 설정을 확인하세요."
                report = command.env.get("PLAYWRIGHT_JUNIT_OUTPUT_FILE")
            elif command.argv != item.command.argv:
                # Vitest/user-defined scripts may configure reporters in files;
                # do not claim an exact output path from unrelated CLI arguments.
                return None
            else:
                report = item.command.junit_report.removeprefix(prefix)
            known = True
            break
    if known:
        if not report:
            return f"현재 명령은 JUnit 파일을 생성하도록 설정되지 않았지만 {command.junit_report} 수집을 요구합니다."
        actual = prefix + report
        if PurePosixPath(actual) != PurePosixPath(command.junit_report):
            return f"명령이 생성하는 JUnit 경로 {actual}와 수집 경로 {command.junit_report}가 다릅니다."
    return None


def policy_warnings(config, info):
    """Explain known tooling mismatches without replacing the saved host policy."""
    try:
        detected = detect_validation(info)
    except (ApplicationError, ValueError) as error:
        return [f"검증 도구 자동 비교를 완료하지 못했습니다: {error}"]
    available = {c.id.split("_", 1)[0] for c in detected.candidates}
    available = {
        "node" if tool in {"node-test", "playwright", "build"} else tool for tool in available
    }
    warnings = []
    for key, command in config.tests.commands.items():
        executable = Path(command.argv[0]).name
        tool = (
            "pytest"
            if "pytest" in command.argv
            else "maven"
            if executable in {"mvn", "mvnw"}
            else "gradle"
            if executable in {"gradle", "gradlew"}
            else "node"
            if executable in {"node", "npm", "pnpm", "yarn"}
            else None
        )
        if available and tool and tool not in available:
            warnings.append(
                f"저장된 검증 {key}는 {tool} 명령이지만 기준 브랜치에서 해당 도구를 감지하지 못했습니다. "
                "프로젝트 검증 설정에서 자동 감지·선택 후 새 작업을 만드세요. 이 작업의 재개 답변으로 명령은 변경되지 않습니다."
            )
        for item in detected.candidates:
            if item.command.working_directory != command.working_directory or script_identity(
                item.command.argv
            ) != script_identity(command.argv):
                continue
            for output in item.output_paths:
                if output.endswith("node_modules/.vite-temp") and not any(
                    PurePosixPath(output).is_relative_to(PurePosixPath(p))
                    for p in config.tests.output_paths
                ):
                    warnings.append(
                        f"저장된 검증 {key}: Vite 임시 출력 경로 {output}를 tests.output_paths에 추가하고 새 작업을 만드세요."
                    )
        if command.kind == "test":
            mismatch = report_warning(command, detected.candidates)
            if mismatch:
                warnings.append(
                    f"저장된 검증 {key}: {mismatch} 프로젝트 검증 설정에서 명령·JUnit 경로를 확인하고 새 작업을 만드세요. "
                    "기존 작업의 재개 답변으로 검증 정책은 변경되지 않습니다."
                )
            report = command.junit_report
            if not any(c in report for c in "*?[") and not any(
                PurePosixPath(report).is_relative_to(PurePosixPath(p))
                for p in config.tests.output_paths
            ):
                warnings.append(
                    f"저장된 검증 {key}: JUnit 수집 경로 {report}를 포함하는 테스트 출력 경로를 설정하세요."
                )
    return warnings
