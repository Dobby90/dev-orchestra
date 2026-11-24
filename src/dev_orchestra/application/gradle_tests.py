"""Translate Gradle test files into exact task and class selectors."""

import re
from pathlib import Path, PurePosixPath

from dev_orchestra.application.gradle_projects import SETTINGS, GradleProjects
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.java import GRADLE_DIRECTORY_TASK, java_tool, wrapper_argv

IDENTIFIER = r"[A-Za-z_][A-Za-z_0-9]*"
CLASS_NAME = rf"{IDENTIFIER}(?:\.{IDENTIFIER})*"
SOURCE_DIRS = {".java": "java", ".kt": "kotlin", ".groovy": "groovy"}
VALUE_OPTIONS = {"-g", "--gradle-user-home", "--project-cache-dir", "--max-workers", "--console"}
SWITCHES = {
    "--offline",
    "--no-daemon",
    "--daemon",
    "--rerun-tasks",
    "--rerun",
    "--no-build-cache",
    "--build-cache",
    "--stacktrace",
    "-s",
    "--full-stacktrace",
    "-S",
    "--info",
    "-i",
    "--warn",
    "-w",
    "--quiet",
    "-q",
    "--parallel",
    "--no-parallel",
    "--configuration-cache",
    "--no-configuration-cache",
}


def gradle_target_class(path, content):
    if path.parts[:3] != ("src", "test", SOURCE_DIRS[path.suffix]):
        raise PipelineError(
            "Gradle targets must be class files in a standard src/test source directory"
        )
    name = path.stem
    # Ignore comments so an obsolete package or class declaration cannot supply a filter.
    source = re.sub(r"/\*[\s\S]*?\*/|//[^\n]*", "", content)
    package = re.search(r"(?m)^\s*package[ \t]+([^;\s]+)", source)
    if (
        not re.fullmatch(IDENTIFIER, name)
        or not package
        or not package[1][0].islower()
        or not re.fullmatch(CLASS_NAME, package[1])
        or not re.search(rf"\bclass\s+{re.escape(name)}\b", source)
    ):
        raise PipelineError(
            "Gradle test file must declare a class matching its filename in a named package "
            "starting with a lowercase letter"
        )
    return f"{package[1]}.{name}"


class GradleSelection:
    def __init__(self, run, candidate, *, project_graph=False):
        self.git = Git(Path(run["repo"]))
        self.candidate = candidate
        self.projects = GradleProjects(self.git, candidate) if project_graph else None
        self.project_paths = {}

    def affected_modules(self, commands, paths):
        if not paths:
            return None
        modules = {
            self.command_directory(command)
            for command in commands
            if java_tool(command) == "gradle"
        }
        if self.projects:
            try:
                if self.projects.settings_inputs().intersection(map(PurePosixPath, paths)):
                    return None
                modules |= self.projects.modules
            except (PipelineError, ValueError):
                return None
        affected = set()
        for name in paths:
            path = PurePosixPath(name)
            if (
                path.name in SETTINGS
                or set(path.parts) & {"buildSrc", "build-logic", "gradle"}
                or path.name in {"gradlew", "gradlew.bat"}
                or len(path.parts) == 1
                and path.name in {"build.gradle", "build.gradle.kts", "gradle.properties"}
            ):
                return None
            owner = next((parent for parent in path.parents if parent in modules), None)
            if owner is not None:
                affected.add(owner)
            elif path.suffix in {".gradle", ".kts"} or path.name == "gradle.properties":
                return None
        return self.projects.dependents(modules, affected) if self.projects else affected

    def command_directory(self, command):
        directory = PurePosixPath(command.working_directory)
        task = "assemble" if command.kind == "check" else "test"
        index = gradle_task_index(command.argv, task)
        if self.projects and index is not None and command.argv[index].startswith(":"):
            try:
                root = self.projects.root(directory)
                path = command.argv[index].rpartition(":")[0]
                if not path:
                    return root
                mapped = self.projects.directory(root, path)
                if mapped in self.projects.modules or directory == root:
                    return mapped
            except (PipelineError, ValueError):
                pass  # Retain the explicit working directory when discovery fails.
            return directory
        if (
            index is not None
            and command.argv[index].startswith(":")
            and self.project_path(directory) == ":"
        ):
            # Root-based commands may name a subproject in their qualified task.
            return directory.joinpath(*command.argv[index].split(":")[1:-1])
        return directory

    def can_scope_check(self, command):
        if not self.projects:
            return True
        index = gradle_task_index(command.argv, "assemble")
        return index is not None and (
            not command.argv[index].startswith(":")
            or self.command_directory(command) in self.projects.modules
        )

    def project_path(self, directory):
        if directory not in self.project_paths:
            parents = (directory, *directory.parents)
            names = [str(parent / name) for parent in parents for name in sorted(SETTINGS)]
            present = set(
                self.git.call("ls-tree", "--name-only", "-z", self.candidate, "--", *names).split(
                    "\0"
                )
            )
            root = next(
                (
                    parent
                    for parent in parents
                    if any(str(parent / name) in present for name in SETTINGS)
                ),
                directory,
            )
            relative = directory.relative_to(root)
            if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in relative.parts):
                raise PipelineError("configure an explicit Gradle task path for this project")
            self.project_paths[directory] = ":".join(("", *relative.parts, ""))
        return self.project_paths[directory]

    def task_argv(self, command, task):
        """Qualify a single task so Gradle does not select descendant projects."""
        argv = list(command.argv)
        index = gradle_task_index(argv, task)
        if index is None:
            return None
        if argv[index] == task:
            if self.projects:
                return argv + [GRADLE_DIRECTORY_TASK + task]
            argv[index] = self.project_path(PurePosixPath(command.working_directory)) + task
        return argv


def gradle_task_index(argv, task):
    start = len(argv) - len(wrapper_argv(argv)) + 1
    args = iter(enumerate(argv[start:], start))
    found = None
    for index, arg in args:
        option, equals, _ = arg.partition("=")
        if option in VALUE_OPTIONS:
            if not equals and next(args, None) is None:
                return None
        elif arg in SWITCHES or re.fullmatch(r"-[DP][A-Za-z_][\w.-]*(?:=.*)?", arg):
            continue
        elif found is None and re.fullmatch(
            rf"(?:(?::[A-Za-z0-9_-][A-Za-z0-9_.-]*)*:)?{task}", arg
        ):
            found = index
        else:
            return None
    return found
