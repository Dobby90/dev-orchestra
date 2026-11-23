"""Read committed Gradle build inputs and literal project dependencies."""

import re
from functools import cached_property
from pathlib import PurePosixPath

SETTINGS = {"settings.gradle", "settings.gradle.kts"}
BUILDS = {"build.gradle", "build.gradle.kts"}
PROJECT_PATH = r"(?:\:[A-Za-z0-9_-][A-Za-z0-9_.-]*)+"
PROJECT_CALL = re.compile(r"\bproject\s*\(")
LITERAL_PROJECT = re.compile(rf"""\s*(?:path\s*[:=]\s*)?(['"])({PROJECT_PATH})\1\s*(?=[),])""")
PROJECT_DIR = re.compile(
    rf"""\bproject\s*\(\s*(['"])({PROJECT_PATH})\1\s*\)\.projectDir\s*=\s*file\s*\(\s*(['"])([^'"$]+)\3"""
)
FILE_INPUT = re.compile(r"""\b(?:file\s*\(\s*|from\s*[:=]\s*)(['"])([^'"$\r\n]+)\1""")


class GradleProjects:
    def __init__(self, git, candidate):
        self.git = git
        self.candidate = candidate

    @cached_property
    def scripts(self):
        # One Git read for all modules. Git ignores symlink and binary entries here.
        listing = self.git.call(
            "grep",
            "-I",
            "-n",
            "-z",
            "-e",
            "^",
            self.candidate,
            "--",
            ":(glob)**/*.gradle",
            ":(glob)**/*.gradle.kts",
        )
        lines = {}
        for line in listing.split("\n"):
            name, _, content = line.split("\0", 2)
            lines.setdefault(PurePosixPath(name.split(":", 1)[1]), []).append(content)
        return {
            path: re.sub(r"/\*[\s\S]*?\*/|(?m:^\s*//.*$)", "", "\n".join(content))
            for path, content in lines.items()
        }

    @cached_property
    def settings(self):
        return {path.parent: text for path, text in self.scripts.items() if path.name in SETTINGS}

    @cached_property
    def modules(self):
        return {path.parent for path in self.scripts if path.name in BUILDS} | {
            path for path in self.mappings.values()
        }

    @cached_property
    def mappings(self):
        return {
            (root, path): root / directory
            for root, text in self.settings.items()
            for _, path, _, directory in PROJECT_DIR.findall(text)
            if safe_relative(directory)
        }

    def root(self, directory):
        return next((p for p in (directory, *directory.parents) if p in self.settings), directory)

    def directory(self, root, path):
        return self.mappings.get((root, path), root.joinpath(*path.split(":")[1:]))

    def settings_inputs(self):
        paths = set()
        pending = list(self.settings.items())
        while pending:
            root, text = pending.pop()
            for _, name in FILE_INPUT.findall(text):
                path = root / name
                if safe_relative(name) and path not in paths:
                    paths.add(path)
                    if path in self.scripts:
                        pending.append((root, self.scripts[path]))
        return paths

    def dependents(self, modules, affected):
        edges = {}
        for path, text in self.scripts.items():
            if path.name not in BUILDS or path.parent not in modules:
                continue
            owner = path.parent
            root = self.root(owner)
            upstream = set()
            for match in PROJECT_CALL.finditer(text):
                literal = LITERAL_PROJECT.match(text, match.end())
                if literal:
                    target = self.directory(root, literal[2])
                    if target in modules:
                        upstream.add(target)
                        continue
                    # Dynamic descriptors may remap a logical child anywhere in its
                    # configured parent module. Retain its consumers for that parent.
                    parent = next((p for p in target.parents if p in modules and p != root), root)
                else:
                    parent = root
                upstream.update(
                    p for p in modules if self.root(p) == root and p.is_relative_to(parent)
                )
            if re.search(r"\bprojects\.", text):
                upstream.update(p for p in modules if self.root(p) == root)
            edges.setdefault(owner, set()).update(upstream)
        while True:
            expanded = affected | {owner for owner, inputs in edges.items() if inputs & affected}
            if expanded == affected:
                return affected
            affected = expanded


def safe_relative(value):
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and bool(path.parts)
