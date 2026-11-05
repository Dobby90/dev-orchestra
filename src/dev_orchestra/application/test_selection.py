"""Deterministic, snapshotted command selection; model output cannot shrink policy."""

from fnmatch import fnmatchcase

from dev_orchestra.application.test_changes import checksum
from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.git import Git

SELECTOR_VERSION = 1


def selection_for_paths(run, candidate, paths, input_error=None):
    tests = Config.model_validate(run["config"]).tests
    scope = run.get("test_scope", "full")
    if scope not in {"full", "affected"}:
        raise PipelineError("unsupported test scope")
    all_ids = set(tests.commands)
    chosen, reasons = set(all_ids), []
    policy = tests.affected
    if scope == "affected":
        if input_error:
            reasons.append("영향 판단에 실패해 전체 검사로 확대합니다.")
        elif not policy:
            reasons.append("영향 범위 선택 정책이 없어 전체 검사로 확대합니다.")
        elif not paths:
            reasons.append("누적 변경이 없어 영향 범위를 확정하지 못해 전체 검사로 확대합니다.")
        elif any(fnmatchcase(path, pattern) for path in paths for pattern in policy.global_paths):
            reasons.append("공통 코드·의존성·검증 설정 변경으로 전체 검사로 확대합니다.")
        elif any(
            not any(
                fnmatchcase(path, pattern)
                for patterns in policy.inputs.values()
                for pattern in patterns
            )
            for path in paths
        ):
            reasons.append("매핑되지 않은 변경 파일이 있어 전체 검사로 확대합니다.")
        else:
            chosen = {
                key
                for key, patterns in policy.inputs.items()
                if any(fnmatchcase(path, pattern) for path in paths for pattern in patterns)
            }
            # command dependencies name upstream commands whose inputs influence this command.
            while True:
                expanded = chosen | {
                    key for key, deps in policy.dependencies.items() if set(deps) & chosen
                }
                if expanded == chosen:
                    break
                chosen = expanded
            if not chosen or not any(tests.commands[key].kind == "test" for key in chosen):
                reasons.append("선택된 실제 테스트가 없어 전체 검사로 확대합니다.")
            chosen |= set(policy.required_commands)
            chosen |= {key for key, command in tests.commands.items() if command.kind == "check"}
            criteria = {c["id"] for c in run.get("criteria", [])}
            if not criteria or any(
                not policy.criterion_commands.get(key)
                or not any(
                    cmd in chosen and tests.commands[cmd].kind == "test"
                    for cmd in policy.criterion_commands[key]
                )
                for key in criteria
            ):
                reasons.append("인수 조건의 실제 테스트 매핑이 부족해 전체 검사로 확대합니다.")
            if reasons:
                chosen = all_ids
    selected = sorted(chosen)
    decisions = {}
    for key, command in tests.commands.items():
        if key not in chosen:
            decisions[key] = (
                "누적 변경의 입력·의존 관계와 필수 회귀 집합에 포함되지 않아 제외했습니다."
            )
        elif scope == "full" or reasons:
            decisions[key] = "전체 설정 검사에 포함했습니다."
        elif command.kind == "check":
            decisions[key] = "필수 빌드·정적 검사입니다."
        elif key in policy.required_commands:
            decisions[key] = "프로젝트의 필수 회귀 검사입니다."
        elif any(
            fnmatchcase(path, pattern) for path in paths for pattern in policy.inputs.get(key, [])
        ):
            decisions[key] = "누적 변경이 명령의 입력 경로와 연결됩니다."
        else:
            decisions[key] = "변경의 직접·간접 명령 의존 관계에 포함됩니다."
    value = {
        "selector_version": SELECTOR_VERSION,
        "input_error": input_error,
        "requested_scope": scope,
        "actual_scope": "full" if scope == "full" or reasons else "affected",
        "expanded": bool(reasons),
        "reasons": reasons
        or [
            "설정된 전체 검사입니다."
            if scope == "full"
            else "누적 변경·의존 관계·필수 회귀 정책으로 선택했습니다."
        ],
        "included": selected,
        "command_reasons": decisions,
        "excluded": sorted(all_ids - chosen),
        "changed_files": sorted(paths),
        "base_sha": run["base_sha"],
        "candidate_sha": candidate,
        "revision": run.get("revision", 0),
        "policy_hash": checksum(run["config"]["tests"]),
        "criteria_hash": checksum(run.get("criteria", [])),
        "commands_hash": checksum({key: tests.commands[key].model_dump() for key in selected}),
    }
    return value | {"selection_hash": checksum(value)}


def select_tests(run, worktree, candidate):
    try:
        names = Git(worktree).call("diff", "--name-only", "-z", run["base_sha"], candidate, "--")
        return selection_for_paths(run, candidate, list(filter(None, names.split("\0"))))
    except PipelineError:
        return selection_for_paths(run, candidate, [], input_error="git_diff_unavailable")


def selected_config(run, selection):
    config = Config.model_validate(run["config"]).tests
    return config.model_copy(
        update={
            "commands": {
                key: command
                for key, command in config.commands.items()
                if key in selection["included"]
            }
        }
    )


def verify_selection(run, payload):
    selection = payload.get("test_selection")
    if not selection:
        if run.get("test_selection_contract"):
            raise PipelineError("missing host test selection evidence")
        return
    expected = selection_for_paths(
        run, payload["sha"], selection["changed_files"], selection.get("input_error")
    )
    if selection != expected or payload.get("revision", 0) != run.get("revision", 0):
        raise PipelineError("test selection SHA, scope, revision or policy mismatch")
    execution = payload.get("execution", {})
    actual = [item.get("command_id") for item in execution.get("commands", [])]
    if set(actual) != set(selection["included"]) or len(actual) != len(set(actual)):
        raise PipelineError("test execution does not cover the host required command set")
    for command in execution.get("commands", []):
        policy = run["config"]["tests"]["commands"][command["command_id"]]
        if command.get("argv") != policy["argv"] or command.get("kind", "test") != policy.get(
            "kind", "test"
        ):
            raise PipelineError("test execution command differs from immutable policy")


def verify_tool_evidence(run, step, payload):
    """Verify the tool result and its JUnit/adapter files independently at approval."""
    import json
    from pathlib import Path

    from dev_orchestra.infrastructure.files import contained, digest

    reference = payload.get("test_evidence_reference")
    if not reference:
        return
    root = Path(step["data"]["artifact"]).parent
    path = contained(root, reference["id"] + "/evidence.json")
    if digest(path.read_bytes()) != reference["hash"]:
        raise PipelineError("host test tool evidence checksum mismatch")
    value = json.loads(path.read_text())

    def normalize(execution):
        return {
            **execution,
            "commands": [
                {k: v for k, v in command.items() if k not in {"junit_path", "junit_artifact_id"}}
                for command in execution["commands"]
            ],
        }

    if (
        value["sha"] != payload["sha"]
        or value["revision"] != payload.get("revision", 0)
        or value["selection"] != payload.get("test_selection")
        or normalize(value["execution"]) != normalize(payload["execution"])
    ):
        raise PipelineError("host test tool result differs from stage evidence")
    for command in value["execution"]["commands"]:
        if command.get("junit_path"):
            junit = contained(
                path.parent, str(Path(command["junit_path"]).relative_to(path.parent))
            )
            if digest(junit.read_bytes()) != command["junit_hash"]:
                raise PipelineError("JUnit evidence checksum mismatch")
        for adapter in command.get("runtime_adapters", []):
            files = list(path.parent.glob(adapter["sha256"][:16] + "-*"))
            if not files or not any(
                digest(file.read_bytes()) == adapter["sha256"] for file in files
            ):
                raise PipelineError("runtime adapter checksum mismatch")
