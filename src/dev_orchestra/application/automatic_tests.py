"""Model-selected targets, translated by the host without accepting arbitrary argv."""

import json
import re
import shlex
from pathlib import Path, PurePosixPath

from dev_orchestra.application.test_changes import checksum
from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError, TestTarget
from dev_orchestra.infrastructure.git import Git


def automatic(run):
    return run.get("test_selection_contract") == 2 and run.get("test_scope") == "affected"


def candidate_file(run, candidate, name):
    root = Path(run["repo"])
    git = Git(root)
    entry = git.call("ls-tree", candidate, "--", name)
    if not entry.startswith(("100644 ", "100755 ")):
        raise PipelineError("test input must be a committed regular file")
    return git.call("show", f"{candidate}:{name}")


def targeted_argv(run, candidate, command, files):
    """Keep trusted runner options; replace positional test paths with exact files."""
    if not files:
        return command.argv
    cwd = PurePosixPath(command.working_directory)
    for name in files:
        path = PurePosixPath(name)
        if (
            not re.fullmatch(r"[A-Za-z0-9_./-]+", name)
            or path.is_absolute()
            or ".." in path.parts
            or name.startswith("-")
            or path.suffix not in {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
        ):
            raise PipelineError("test target must be an exact relative test file")
        content = candidate_file(run, candidate, str(cwd / path))
        if not content:
            raise PipelineError("test target is empty")
    argv = list(command.argv)
    if Path(argv[0]).name == "npm":
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
            if runner[0] == "npx":
                runner = runner[1:]
            if runner[0] in {"playwright", "vitest"}:
                runner[0] = "./node_modules/.bin/" + runner[0]
            node_options = [
                option
                for arg in argv[1:index]
                if arg.startswith("--node-options=")
                for option in shlex.split(arg.split("=", 1)[1])
            ]
            if node_options and runner[0] != "node":
                raise ValueError("node options require node runner")
            extra = argv[index + 2 :]
            argv = (
                runner[:1]
                + node_options
                + runner[1:]
                + (extra[1:] if extra[:1] == ["--"] else extra)
            )
            # Preserve npm lifecycle behavior: targeting scripts with hooks needs a dedicated command.
            if "pre" + script in package["scripts"] or "post" + script in package["scripts"]:
                raise ValueError("npm lifecycle hooks")
        except (ValueError, KeyError, IndexError) as error:
            raise PipelineError(
                "cannot safely filter npm script; select this command with files=[] and explain why"
            ) from error
    executable = Path(argv[0]).name
    prefix = 1
    if executable.startswith("python") and argv[1:3] == ["-m", "pytest"]:
        prefix = 3
        executable = "pytest"
    elif executable == "node" and "--test" not in argv:
        raise PipelineError("node target requires --test")
    elif executable in {"playwright", "vitest"}:
        if argv[1:2] not in (["test"], ["run"]):
            raise PipelineError("test runner requires test/run subcommand")
        prefix = 2
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
    return result


def model_selection(run, candidate, paths, plan=None, input_error=None):
    tests = Config.model_validate(run["config"]).tests
    if input_error:
        raise PipelineError(
            "cannot analyze cumulative changes; refusing automatic full test fallback"
        )
    if plan is None:
        return {
            "selector_version": 2,
            "included": [],
            "changed_files": sorted(paths),
            "available_commands": {
                key: value.model_dump() for key, value in tests.commands.items()
            },
            "instructions": "Analyze cumulative diff, callers, dependencies and tests. Supply test_plan with command_id, files (relative to command working_directory), reason. No static mapping required. Use exact test files; files=[] expands one command only with a concrete reason. Cover every criterion and changed behavior. Mandatory checks/regressions remain host-owned.",
        }
    targets = [TestTarget.model_validate(item) for item in plan]
    if any(not item.reason.strip() or len(item.files) != len(set(item.files)) for item in targets):
        raise PipelineError("test targets require a reason and unique files")
    ids = [item.command_id for item in targets]
    if not targets or len(ids) != len(set(ids)) or not set(ids) <= set(tests.commands):
        raise PipelineError("automatic test plan requires unique configured command IDs")
    if not any(tests.commands[key].kind == "test" for key in ids):
        raise PipelineError("automatic test plan must execute real tests")
    chosen = set(ids) | {key for key, command in tests.commands.items() if command.kind == "check"}
    policy = tests.affected
    if policy:
        chosen |= set(policy.required_commands)
        while True:
            expanded = chosen | {
                key for key, deps in policy.dependencies.items() if set(deps) & chosen
            }
            if expanded == chosen:
                break
            chosen = expanded
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
        argv = targeted_argv(run, candidate, command, item.files) if item else command.argv
        commands[key] = command.model_copy(update={"argv": argv}).model_dump()
        decisions[key] = (
            item.reason + " 대상: " + (", ".join(item.files) if item.files else "명령 전체")
            if item
            else "필수 정적 검사·회귀·의존 명령입니다."
        )
    for key in set(tests.commands) - chosen:
        decisions[key] = "모델이 누적 변경·의존 관계·인수 조건을 분석해 실행 대상에서 제외했습니다."
    broad = any(
        not item.files and tests.commands[item.command_id].kind == "test" for item in targets
    )
    value = {
        "selector_version": 2,
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
        "policy_hash": checksum(run["config"]["tests"]),
        "criteria_hash": checksum(run.get("criteria", [])),
        "commands_hash": checksum(commands),
        "test_plan": [item.model_dump() for item in targets],
        "resolved_commands": commands,
    }
    return value | {"selection_hash": checksum(value)}
