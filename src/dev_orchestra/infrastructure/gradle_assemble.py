"""Batch compatible host-selected Gradle assemble checks with task-level evidence."""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from dev_orchestra.config import TestCommand
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained

ASSEMBLE_INIT = """
import groovy.json.JsonOutput
import groovy.json.JsonSlurper

if (gradle.parent == null) {
    def input = new JsonSlurper().parseText(__INPUT__)
    def targets = [:]
    def emit = { value -> println(input.marker + JsonOutput.toJson(value)) }
    gradle.settingsEvaluated { settings ->
        if (settings.settingsDir.canonicalPath != input.root) {
            throw new GradleException('Unexpected assemble batch settings directory')
        }
        def projects = [:]
        def pending = [settings.rootProject]
        while (pending) {
            def project = pending.remove(pending.size() - 1)
            projects[project.path] = project.projectDir.canonicalPath
            pending.addAll(project.children)
        }
        input.targets.each { id, target ->
            def paths = target.directory ? projects.findAll { path, dir -> dir == target.directory }.keySet() : [target.project]
            if (paths.size() != 1 || !projects.containsKey(paths.first())) {
                throw new GradleException('Cannot identify one project for an assemble check')
            }
            targets[id] = (paths.first() == ':' ? '' : paths.first()) + ':assemble'
        }
        if (targets.values().toSet().size() != targets.size()) {
            throw new GradleException('Assemble checks must identify distinct tasks')
        }
        gradle.startParameter.setTaskNames(targets.values().toList())
    }
    gradle.taskGraph.whenReady { graph ->
        def tasks = graph.allTasks.collectEntries { task -> [(task.path): task] }
        def dependencies = targets.collectEntries { id, path ->
            if (!tasks.containsKey(path)) throw new GradleException('Missing assemble task in the graph')
            def seen = [] as Set
            def pending = [tasks[path]]
            while (pending) {
                def task = pending.remove(pending.size() - 1)
                if (seen.add(task.path)) pending.addAll(graph.getDependencies(task))
            }
            [(id): [task: path, dependencies: seen as List]]
        }
        emit([targets: dependencies])
    }
    gradle.taskGraph.afterTask { task, state ->
        if (state.failure != null || targets.containsValue(task.path)) {
            emit([task: task.path, failed: state.failure != null])
        }
    }
}
"""


@dataclass(frozen=True)
class AssembleCheck:
    id: str
    command: TestCommand
    cwd: Path
    root: Path
    selector: str
    launcher: tuple[str, ...]
    options: tuple[str, ...]
    environment: dict[str, str]
    caches: tuple[Path, ...]
    seed: dict | None


def _check(worktree, command_id, command, roots, bindings):
    from dev_orchestra.application.gradle_tests import gradle_task_index
    from dev_orchestra.infrastructure.java import (
        GRADLE_DIRECTORY_TASK,
        cache_paths,
        java_argv,
        java_environment,
        java_tool,
        wrapper_argv,
    )

    if (
        command.kind != "check"
        or java_tool(command) != "gradle"
        or command.env.keys() - {"JAVA_HOME"}
    ):
        return None
    index = gradle_task_index(command.argv, "assemble")
    if index is None:
        return None
    task = command.argv[index]
    marker = GRADLE_DIRECTORY_TASK + "assemble"
    directory_task = task == "assemble" and command.argv.count(marker) == 1
    if not directory_task and (not task.startswith(":") or marker in command.argv):
        return None
    start = len(command.argv) - len(wrapper_argv(command.argv)) + 1
    args = iter(
        arg for i, arg in enumerate(command.argv) if i >= start and i != index and arg != marker
    )
    flags = []
    cache_options = set()
    for arg in args:
        option, separator, value = arg.partition("=")
        if option in {"--gradle-user-home", "-g", "--project-cache-dir"}:
            key = "--gradle-user-home" if option == "-g" else option
            if key in cache_options or not (value if separator else next(args, None)):
                return None
            cache_options.add(key)
            continue
        if arg not in {"--offline", "--no-daemon", "--console=plain", "--no-configuration-cache"}:
            return None
        flags.append(arg)
    if len(cache_options) != 2 or not {"--offline", "--no-daemon"}.issubset(flags):
        return None

    cwd = (
        worktree
        if command.working_directory == "."
        else contained(worktree, command.working_directory)
    )
    root = roots.build_root(worktree, cwd)
    if not any((root / name).is_file() for name in ("settings.gradle", "settings.gradle.kts")):
        return None
    argv = java_argv(worktree, command)
    executable = wrapper_argv(argv)[0]
    if Path(executable).name == "gradlew":
        path = (cwd / executable).resolve()
        path = contained(worktree, path.relative_to(worktree).as_posix())
        launcher = ("/bin/sh", str(path)) if argv[0] == "/bin/sh" else (str(path),)
    else:
        launcher = (executable,)
    env = java_environment(worktree, command)
    env.pop("GRADLE_USER_HOME", None)
    return AssembleCheck(
        command_id,
        command,
        cwd,
        root,
        str(cwd) if directory_task else task,
        launcher,
        tuple(sorted(set(flags))),
        env,
        tuple(
            contained(worktree, (cwd / path).relative_to(worktree).as_posix())
            for path in cache_paths(command)
        ),
        bindings.commands.get(command_id) if bindings else None,
    )


def _managed_cache(check):
    home, project = check.caches
    return (
        home == check.cwd / ".devorchestra-cache/gradle"
        and project == check.cwd / ".devorchestra-cache/project"
        and not any(
            (home / path).exists() or (home / path).is_symlink()
            for path in (
                "gradle.properties",
                "init.gradle",
                "init.gradle.kts",
                "init.d",
            )
        )
        and not any((home / "caches/modules-2/files-2.1").glob("*"))
    )


def compatible_caches(first, next_check):
    # Recheck private overlays just before launch, after any earlier commands ran.
    return (
        first.caches == next_check.caches
        and first.cwd == next_check.cwd
        or (first.seed is not None and _managed_cache(first) and _managed_cache(next_check))
    )


def _compatible(first, next_check, *, native_tools):
    return (
        first.root == next_check.root
        and first.launcher == next_check.launcher
        and first.options == next_check.options
        and first.command.env == next_check.command.env
        and first.environment == next_check.environment
        and first.command.network == next_check.command.network
        and first.command.timeout_seconds == next_check.command.timeout_seconds
        and first.seed == next_check.seed
        and compatible_caches(first, next_check)
        and (first.cwd == next_check.cwd or not native_tools())
    )


def assemble_batches(worktree, commands, directory, bindings, stop):
    from functools import cache

    from dev_orchestra.infrastructure.containers import MANIFEST
    from dev_orchestra.infrastructure.java_cache import GradleSeeds, check_stop
    from dev_orchestra.infrastructure.java_native import declarations

    manifest = worktree / MANIFEST
    if manifest.exists() or manifest.is_symlink():
        return {}

    @cache
    def native_tools():
        return declarations(worktree)

    roots = GradleSeeds(directory)
    batches = {}
    group = []
    for key, command in commands.items():
        check_stop(stop)
        try:
            candidate = _check(worktree, key, command, roots, bindings)
            compatible = (
                candidate and group and _compatible(group[0], candidate, native_tools=native_tools)
            )
        except (PipelineError, OSError, ValueError):
            candidate, compatible = None, False
        if group and (not compatible or any(item.selector == candidate.selector for item in group)):
            if len(group) > 1:
                batches[group[0].id] = group
            group = []
        if candidate:
            group.append(candidate)
    if len(group) > 1:
        batches[group[0].id] = group
    return batches


def batch_adapter(batch, directory):
    from dev_orchestra.infrastructure.java_native import groovy_literal
    from dev_orchestra.infrastructure.runtime_adapters import setup_file

    marker = "DEVORCHESTRA_ASSEMBLE_" + uuid.uuid4().hex + " "
    targets = {
        check.id: {"directory": check.selector}
        if check.selector.startswith("/")
        else {"project": check.selector.rpartition(":")[0] or ":"}
        for check in batch
    }
    value = {"root": str(batch[0].root), "targets": targets, "marker": marker}
    path, sha = setup_file(
        directory,
        "gradle-assemble.gradle",
        ASSEMBLE_INIT.replace("__INPUT__", groovy_literal(json.dumps(value))),
    )
    return path, sha, marker


def batch_outcomes(batch, marker, output, exit_code):
    """Return one outcome per command, or require individual runs on ambiguous evidence."""
    targets = None
    tasks = {}
    try:
        for line in output.splitlines():
            if not line.startswith(marker):
                continue
            row = json.loads(line.removeprefix(marker))
            if set(row) == {"targets"} and targets is None and not tasks:
                targets = row["targets"]
            elif set(row) == {"task", "failed"} and targets is not None:
                if row["task"] in tasks or not isinstance(row["failed"], bool):
                    return None
                tasks[row["task"]] = row["failed"]
            else:
                return None
        if not targets or set(targets) != {check.id for check in batch}:
            return None
        failed = {task for task, failed in tasks.items() if failed}
        attributed = set()
        outcomes = {}
        for check in batch:
            target = targets[check.id]["task"]
            deps = set(targets[check.id]["dependencies"])
            if check.selector.startswith(":") and check.selector != target:
                return None
            if target not in deps or not target.endswith(":assemble"):
                return None
            errors = failed & deps
            if target not in tasks and not errors:
                return None
            attributed.update(errors)
            outcomes[check.id] = (target, target in tasks, sorted(errors))
        if len({task for task, _, _ in outcomes.values()}) != len(batch):
            return None
        if attributed != failed or bool(failed) != bool(exit_code):
            return None
        return outcomes
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
