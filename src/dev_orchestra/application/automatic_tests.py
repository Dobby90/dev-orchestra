"""Model-selected targets, translated by the host without accepting arbitrary argv."""

import json
import re
import shlex
from fnmatch import fnmatchcase
from pathlib import Path, PurePosixPath

from dev_orchestra.application.frontend_build_scope import (
    APP_CONFIGURATION,
    frontend_build,
    frontend_bundle_changed,
)
from dev_orchestra.application.gradle_tests import SOURCE_DIRS, GradleSelection, gradle_target_class
from dev_orchestra.application.test_changes import checksum
from dev_orchestra.application.validation import (
    docusaurus_build,
    run_test_policy,
    run_test_policy_hash,
)
from dev_orchestra.domain.models import PipelineError, TestTarget
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.java import java_tool

CRITERION_TEST_CONTRACT = 8
AUTOMATIC_TEST_CONTRACT = CRITERION_TEST_CONTRACT
PROJECT_SCOPE_CONTRACTS = {7, CRITERION_TEST_CONTRACT}
BUNDLE_SCOPE_CONTRACTS = {6, *PROJECT_SCOPE_CONTRACTS}
FRONTEND_PACKAGE_CONTRACTS = {5, *BUNDLE_SCOPE_CONTRACTS}
JEST_TEST_CONTRACTS = {4, *FRONTEND_PACKAGE_CONTRACTS}
GRADLE_TEST_CONTRACTS = {3, *JEST_TEST_CONTRACTS}
AUTOMATIC_TEST_CONTRACTS = {2, *GRADLE_TEST_CONTRACTS}
NODE_LOCKFILES = {"npm": "package-lock.json", "pnpm": "pnpm-lock.yaml", "yarn": "yarn.lock"}


def automatic(run):
    return (
        run.get("test_selection_contract") in AUTOMATIC_TEST_CONTRACTS
        and run.get("test_scope") == "affected"
    )


def prepare_selected_only(run):
    return automatic(run) and run["test_selection_contract"] in PROJECT_SCOPE_CONTRACTS


def criterion_test_commands(run, tests):
    if run.get("test_selection_contract") != CRITERION_TEST_CONTRACT or not tests.affected:
        return {}
    mapping = tests.affected.criterion_commands
    test_ids = {key for key, command in tests.commands.items() if command.kind == "test"}
    return {
        criterion["id"]: sorted(mapped_tests)
        for criterion in run.get("criteria", [])
        if (mapped_tests := test_ids.intersection(mapping.get(criterion["id"], [])))
    }


def candidate_file(run, candidate, name):
    root = Path(run["repo"])
    git = Git(root)
    entry = git.call("ls-tree", candidate, "--", name)
    if not entry.startswith(("100644 ", "100755 ")):
        raise PipelineError("test input must be a committed regular file")
    return git.call("show", f"{candidate}:{name}")


def has_parent_workspace(git, candidate, cwd):
    names = [
        str(parent / name)
        for parent in cwd.parents
        for name in ("package.json", "pnpm-workspace.yaml")
    ]
    if not names:
        return False
    present = git.call("ls-tree", "--name-only", "-z", candidate, "--", *names).split("\0")
    return any(
        name.endswith("pnpm-workspace.yaml")
        or json.loads(git.call("show", f"{candidate}:{name}")).get("workspaces")
        for name in filter(None, present)
    )


def deferred_builds(run, candidate, tests):
    """Identify eligible build commands for selection and on-demand preparation."""
    if not automatic(run) or run["test_selection_contract"] not in JEST_TEST_CONTRACTS:
        return {}
    version = run["test_selection_contract"]
    scoped_frontend = version in FRONTEND_PACKAGE_CONTRACTS
    repo_root = PurePosixPath(".")
    git = Git(Path(run["repo"]))
    deferred = {}
    for key, command in tests.commands.items():
        cwd = PurePosixPath(command.working_directory)
        lockfile = NODE_LOCKFILES.get(Path(command.argv[0]).name)
        script = command.argv[2] if command.argv[1:2] == ["run"] and len(command.argv) == 3 else ""
        if (
            command.kind != "check"
            or cwd == repo_root
            and version not in BUNDLE_SCOPE_CONTRACTS
            or not lockfile
            or script != "build"
            and not (version in PROJECT_SCOPE_CONTRACTS and script == "build:production")
        ):
            continue
        try:
            package = json.loads(candidate_file(run, candidate, str(cwd / "package.json")))
            dependencies = package.get("dependencies", {}) | package.get("devDependencies", {})
            bundle = scoped_frontend and frontend_build(package, dependencies, script)
            if (
                (bundle or script == "build" and cwd != repo_root and docusaurus_build(package))
                and not package.get("workspaces")
                and not {"pre" + script, "post" + script}.intersection(package["scripts"])
                and not any(
                    not isinstance(v, str) or v.startswith(("file:", "link:", "workspace:"))
                    for v in (dependencies | package.get("optionalDependencies", {})).values()
                )
                and git.call("ls-tree", candidate, "--", str(cwd / lockfile)).startswith(
                    ("100644 ", "100755 ")
                )
                and (not scoped_frontend or not has_parent_workspace(git, candidate, cwd))
                and (
                    version not in BUNDLE_SCOPE_CONTRACTS
                    or not git.call("ls-tree", candidate, "--", str(cwd / "pnpm-workspace.yaml"))
                )
            ):
                deferred[key] = "bundle" if bundle else "docs"
        except (PipelineError, ValueError, TypeError, AttributeError):
            continue
    return deferred


def check_jest_options(options):
    switches = {"--ci", "--runInBand", "--json", "--silent", "--verbose", "--no-cache"}
    values = {"--config", "--outputFile", "--cacheDirectory", "--maxWorkers"}
    remaining = iter(options)
    for arg in remaining:
        flag, equals, value = arg.partition("=")
        if flag in values:
            value = value if equals else next(remaining, "")
            if value and not value.startswith("-"):
                continue
        elif arg in switches:
            continue
        raise PipelineError("unsupported Jest option for exact file targeting: " + arg)


def jest_file_args(files):
    if any(PurePosixPath(name).as_posix() != name for name in files):
        raise PipelineError("Jest test target must use a canonical relative path")
    return ["--runTestsByPath", *("./" + name for name in files)]


def targeted_argv(run, candidate, command, files, gradle=None):
    """Keep trusted runner options; replace positional test paths with exact files."""
    if not files:
        return command.argv
    cwd = PurePosixPath(command.working_directory)
    gradle_command = gradle is not None and java_tool(command) == "gradle"
    suffixes = (
        SOURCE_DIRS.keys()
        if gradle_command
        else {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
    )
    classes = []
    module = gradle.command_directory(command) if gradle_command else cwd
    for name in files:
        path = PurePosixPath(name)
        if (
            not re.fullmatch(r"[A-Za-z0-9_./-]+", name)
            or path.is_absolute()
            or ".." in path.parts
            or gradle_command
            and name != path.as_posix()
            or name.startswith("-")
            or path.suffix not in suffixes
        ):
            raise PipelineError("test target must be an exact relative test file")
        content = candidate_file(run, candidate, str(cwd / path))
        if not content:
            raise PipelineError("test target is empty")
        if gradle_command:
            source = cwd / path
            if not source.is_relative_to(module):
                raise PipelineError("Gradle test file must belong to the selected module")
            classes.append(gradle_target_class(source.relative_to(module), content))
    if gradle_command:
        if len(classes) != len(set(classes)):
            raise PipelineError("Gradle test targets must name distinct classes")
        argv = gradle.task_argv(command, "test")
        if argv is None:
            raise PipelineError(
                "Gradle class targeting requires one test task without existing filters"
            )
        return argv + [arg for name in classes for arg in ("--tests", name)]
    argv = list(command.argv)
    manager = Path(argv[0]).name
    jest_enabled = run.get("test_selection_contract") in JEST_TEST_CONTRACTS
    exec_prefix = []
    if manager in {"npm", "pnpm", "yarn"} and argv[1:2] == ["exec"]:
        prefix = 4 if manager == "npm" and argv[2:4] == ["--no", "--"] else 2
        exec_prefix, argv = argv[:prefix], argv[prefix:]
        if not argv:
            raise PipelineError("test runner missing after package manager exec")
    elif manager in {"npm", "pnpm", "yarn"}:
        try:
            index = argv.index("run")
            if any(not arg.startswith("--node-options=") for arg in argv[1:index]):
                raise ValueError("unsupported npm prefix options")
            script = argv[index + 1]
            package = json.loads(candidate_file(run, candidate, str(cwd / "package.json")))
            text = package["scripts"][script]
            if any(char in text for char in ";&|<>`$\n"):
                raise ValueError("compound npm script")
            runner = shlex.split(text)
            if "pre" + script in package["scripts"] or "post" + script in package["scripts"]:
                raise ValueError("package manager lifecycle hooks")
            extra = argv[index + 2 :]
            if jest_enabled and runner[0] == "jest":
                if index != 1:
                    raise ValueError("unsupported Jest package manager options")
                separator = ["--"] if manager == "npm" or extra[:1] == ["--"] else []
                options = extra[1:] if extra[:1] == ["--"] else extra
                check_jest_options(runner[1:])
                check_jest_options(options)
                return argv[: index + 2] + separator + options + jest_file_args(files)
            if runner[0] == "npx":
                runner = runner[1:]
            if runner and runner[0] in {"playwright", "vitest"}:
                runner[0] = "./node_modules/.bin/" + runner[0]
            node_options = [
                option
                for arg in argv[1:index]
                if arg.startswith("--node-options=")
                for option in shlex.split(arg.split("=", 1)[1])
            ]
            if node_options and runner[0] != "node":
                raise ValueError("node options require node runner")
            argv = (
                runner[:1]
                + node_options
                + runner[1:]
                + (extra[1:] if extra[:1] == ["--"] else extra)
            )
        except (ValueError, KeyError, IndexError) as error:
            raise PipelineError(
                "cannot safely filter package script; select this command with files=[] and explain why"
            ) from error
    executable = Path(argv[0]).name
    if jest_enabled and executable == "jest" and (exec_prefix or manager not in NODE_LOCKFILES):
        check_jest_options(argv[1:])
        return exec_prefix + argv + jest_file_args(files)
    prefix = 1
    if executable.startswith("python") and argv[1:3] == ["-m", "pytest"]:
        prefix = 3
        executable = "pytest"
    elif executable == "node" and "--test" not in argv:
        raise PipelineError("node target requires --test")
    elif (executable, argv[1:2]) in (("playwright", ["test"]), ("vitest", ["run"])):
        prefix = 2
    elif executable == "playwright" or executable == "vitest" and "--run" not in argv:
        raise PipelineError("test runner requires test/run subcommand")
    if executable not in {"pytest", "node", "playwright", "vitest"}:
        raise PipelineError(
            "runner has no safe file adapter; select files=[] with an expansion reason"
        )
    # Options taking a separate value. Unknown options must use --option=value;
    # this prevents treating an option's value as a positional test path.
    values = {
        "-k",
        "-m",
        "-c",
        "-o",
        "--rootdir",
        "--junitxml",
        "--junit-xml",
        "--confcutdir",
        "--basetemp",
        "--tb",
        "--maxfail",
        "--durations",
        "--reporter",
        "--outputFile",
        "--config",
        "--project",
        "--workers",
        "--timeout",
        "--test-reporter",
        "--test-reporter-destination",
        "--test-name-pattern",
        "--test-concurrency",
    }
    switches = {
        "-q",
        "-v",
        "-vv",
        "-s",
        "-x",
        "--disable-warnings",
        "--strict-markers",
        "--strict-config",
        "--test",
        "--run",
        "--no-cache",
        "--no-file-parallelism",
        "--bail",
    }
    result = argv[:prefix]
    index = prefix
    while index < len(argv):
        arg = argv[index]
        if arg in values:
            if index + 1 == len(argv):
                raise PipelineError("test runner option has no value")
            result.extend(argv[index : index + 2])
            index += 2
            continue
        if arg.startswith("-"):
            if arg not in switches and "=" not in arg:
                raise PipelineError("unsupported target adapter option: " + arg)
            result.append(arg)
        # Existing positional paths/globs are replaced, never appended to.
        index += 1
    if executable == "playwright":
        # Playwright interprets file arguments as regular expressions.
        result.extend(re.escape(name) + "$" for name in files)
    else:
        result.extend(files)
    return exec_prefix + result


def command_dependencies(chosen, policy):
    if policy:
        chosen |= set(policy.required_commands)
        while True:
            expanded = chosen | {
                key for key, deps in policy.dependencies.items() if set(deps) & chosen
            }
            if expanded == chosen:
                break
            chosen = expanded
    return chosen


def mandatory_checks(run, candidate, tests, paths, gradle, deferred):
    checks = {key for key, command in tests.commands.items() if command.kind == "check"}
    policy = tests.affected
    if policy and any(
        fnmatchcase(path, pattern) for path in paths for pattern in policy.global_paths
    ):
        return checks
    version = run.get("test_selection_contract")
    scoped_frontend = version in FRONTEND_PACKAGE_CONTRACTS
    directories = {key: PurePosixPath(tests.commands[key].working_directory) for key in deferred}
    nested = set(directories.values()) - {PurePosixPath(".")}
    root_bundle = version in PROJECT_SCOPE_CONTRACTS and any(
        directories[key] == PurePosixPath(".") and kind == "bundle"
        for key, kind in deferred.items()
    )
    if paths and all(
        PurePosixPath(path).parts[:1] in {("src",), ("tests",)}
        or root_bundle
        and APP_CONFIGURATION.fullmatch(path)
        or scoped_frontend
        and any(PurePosixPath(path).is_relative_to(cwd) for cwd in nested)
        for path in paths
    ):
        for key, cwd in directories.items():
            if policy and any(
                fnmatchcase(path, pattern)
                for path in paths
                for pattern in policy.inputs.get(key, [])
            ):
                continue
            owned = [path for path in paths if PurePosixPath(path).is_relative_to(cwd)]
            if version in BUNDLE_SCOPE_CONTRACTS and deferred[key] == "bundle":
                children = {p for p in nested if p != cwd and p.is_relative_to(cwd)}
                owned = [
                    path
                    for path in owned
                    if not any(PurePosixPath(path).is_relative_to(child) for child in children)
                ]
                if not frontend_bundle_changed(run, candidate, cwd, owned):
                    checks.remove(key)
            elif not owned:
                checks.remove(key)
    if gradle is None:
        return checks
    affected_modules = gradle.affected_modules(tests.commands.values(), paths)
    if affected_modules is None:
        return checks
    return {
        key
        for key in checks
        if java_tool(tests.commands[key]) != "gradle"
        or not gradle.can_scope_check(tests.commands[key])
        or gradle.command_directory(tests.commands[key]) in affected_modules
        or (
            policy
            and any(
                fnmatchcase(path, pattern)
                for path in paths
                for pattern in policy.inputs.get(key, [])
            )
        )
    }


def model_selection(run, candidate, paths, plan=None, input_error=None):
    tests = run_test_policy(run)
    if input_error:
        raise PipelineError(
            "cannot analyze cumulative changes; refusing automatic full test fallback"
        )
    version = run.get("test_selection_contract", 2)
    gradle = (
        GradleSelection(run, candidate, project_graph=version in PROJECT_SCOPE_CONTRACTS)
        if version in GRADLE_TEST_CONTRACTS
        and any(java_tool(c) == "gradle" for c in tests.commands.values())
        else None
    )
    policy = tests.affected
    deferred = deferred_builds(run, candidate, tests)
    checks = mandatory_checks(run, candidate, tests, paths, gradle, deferred)
    if policy:
        eligible = (
            {key for key, command in tests.commands.items() if command.kind == "check"}
            if version in PROJECT_SCOPE_CONTRACTS
            else deferred.keys()
        )
        checks.update(
            eligible
            & {
                key
                for criterion in run.get("criteria", [])
                for key in policy.criterion_commands.get(criterion["id"], [])
            }
        )
    required_criterion_tests = criterion_test_commands(run, tests)
    required_coverage = [
        {"criterion_id": key, "command_ids": alternatives}
        for key, alternatives in required_criterion_tests.items()
    ]
    if plan is None:
        pending = {
            "selector_version": version,
            "included": [],
            "changed_files": sorted(paths),
            "available_commands": {
                key: value.model_dump() for key, value in tests.commands.items()
            },
            "mandatory_command_ids": sorted(command_dependencies(checks.copy(), policy)),
            "dependencies": policy.dependencies if policy else {},
            "instructions": "Analyze cumulative diff, callers, dependencies and tests. Supply test_plan with command_id, files (relative to command working_directory), reason. No static mapping required. Use exact test files; files=[] expands one command only with a concrete reason. Cover every criterion and changed behavior. Mandatory checks/regressions remain host-owned.",
        }
        if version in GRADLE_TEST_CONTRACTS:
            pending["instructions"] += (
                " For Gradle, supply exact Java, Kotlin or Groovy class files under src/test. "
                "The host scopes supported test tasks to the selected classes and includes Gradle "
                "build checks for affected modules. Include mandatory_command_ids and any configured "
                "dependents of your selected commands."
            )
        if version in JEST_TEST_CONTRACTS:
            pending["instructions"] += (
                " Jest supports exact test files. Include builds when the change or acceptance "
                "criteria require bundle verification, for example loader changes, dynamic "
                "imports, emitted assets or compatibility against the production configuration."
                if version in BUNDLE_SCOPE_CONTRACTS
                else " Jest supports exact test files. Include builds when the change or acceptance "
                "criteria affect the generated output of the corresponding package."
                if version in FRONTEND_PACKAGE_CONTRACTS
                else " Jest supports exact test files. Include documentation builds whenever the change "
                "or acceptance criteria affect the generated documentation."
            )
        if required_criterion_tests:
            pending["required_criterion_test_commands"] = required_coverage
            pending["instructions"] += (
                " For each criterion in required_criterion_test_commands, execute at least one "
                "listed test command and cite an executed listed command in that criterion's coverage."
            )
        return pending
    targets = [TestTarget.model_validate(item) for item in plan]
    if any(not item.reason.strip() or len(item.files) != len(set(item.files)) for item in targets):
        raise PipelineError("test targets require a reason and unique files")
    ids = [item.command_id for item in targets]
    if not targets or len(ids) != len(set(ids)) or not set(ids) <= set(tests.commands):
        raise PipelineError("automatic test plan requires unique configured command IDs")
    if not any(tests.commands[key].kind == "test" for key in ids):
        raise PipelineError("automatic test plan must execute real tests")
    chosen = command_dependencies(set(ids) | checks, policy)
    missing = {
        key: alternatives
        for key, alternatives in required_criterion_tests.items()
        if chosen.isdisjoint(alternatives)
    }
    if missing:
        raise PipelineError(
            f"test plan must execute at least one mapped test per acceptance criterion: {missing}"
        )
    commands, decisions = {}, {}
    by_id = {item.command_id: item for item in targets}
    for key in sorted(chosen):
        command = tests.commands[key]
        item = by_id.get(key)
        if item and item.files and command.kind != "test":
            raise PipelineError("static checks cannot accept test file filters")
        # Required regression commands cannot be narrowed by a model.
        if item and item.files and policy and key in policy.required_commands:
            raise PipelineError("required regression command must run unfiltered")
        argv = command.argv
        if item and item.files:
            argv = targeted_argv(run, candidate, command, item.files, gradle)
        elif (
            gradle
            and java_tool(command) == "gradle"
            and not (policy and key in policy.required_commands)
        ):
            task = "assemble" if command.kind == "check" else "test"
            argv = gradle.task_argv(command, task) or argv
        commands[key] = command.model_copy(update={"argv": argv}).model_dump()
        decisions[key] = (
            item.reason + " 대상: " + (", ".join(item.files) if item.files else "명령 전체")
            if item
            else "필수 정적 검사·회귀·의존 명령입니다."
        )
    for key in set(tests.commands) - chosen:
        command = tests.commands[key]
        decisions[key] = (
            "누적 변경의 Gradle 모듈과 프로젝트 검증 정책에 연결되지 않아 제외했습니다."
            if gradle and command.kind == "check" and java_tool(command) == "gradle"
            else "누적 변경과 검증 정책에서 별도 번들 검사가 필요하지 않아 제외했습니다."
            if deferred.get(key) == "bundle" and version in BUNDLE_SCOPE_CONTRACTS
            else "누적 변경과 연결되지 않은 프런트엔드 패키지의 빌드를 제외했습니다."
            if key in deferred and version in FRONTEND_PACKAGE_CONTRACTS
            else "문서 빌드에 영향이 없는 앱 변경으로 검증에서 제외했습니다."
            if key in deferred
            else "모델이 누적 변경·의존 관계·인수 조건을 분석해 실행 대상에서 제외했습니다."
        )
    broad = any(
        not item.files and tests.commands[item.command_id].kind == "test" for item in targets
    )
    value = {
        "selector_version": version,
        "input_error": None,
        "requested_scope": "affected",
        "actual_scope": "full"
        if chosen == set(tests.commands)
        and all(commands[key]["argv"] == tests.commands[key].argv for key in chosen)
        else "affected",
        "expanded": broad,
        "reasons": ["모델이 누적 변경·관련 코드·인수 조건을 분석해 테스트를 선택했습니다."]
        + [item.reason for item in targets if not item.files],
        "included": sorted(chosen),
        "excluded": sorted(set(tests.commands) - chosen),
        "command_reasons": decisions,
        "changed_files": sorted(paths),
        "base_sha": run["base_sha"],
        "candidate_sha": candidate,
        "revision": run.get("revision", 0),
        "policy_hash": run_test_policy_hash(run),
        "criteria_hash": checksum(run.get("criteria", [])),
        "commands_hash": checksum(commands),
        "test_plan": [item.model_dump() for item in targets],
        "resolved_commands": commands,
    }
    if required_criterion_tests:
        value["required_criterion_test_commands"] = required_coverage
    return value | {"selection_hash": checksum(value)}
