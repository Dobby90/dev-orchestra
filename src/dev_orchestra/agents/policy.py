"""Host validation of model-supplied patches before any source mutation."""

import re
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained
from dev_orchestra.infrastructure.git import Git


def in_paths(path, roots):
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


def apply_patch(worktree: Path, patch: str, role: str, test_paths: list[str]):
    if not patch:
        return []
    # Model diffs occasionally indent section headers or miscount hunks. Git recounts
    # hunk sizes; scope and Git-interpreted filenames are still validated below.
    patch = re.sub(r"(?m)^ +(?=diff --git )", "", patch).rstrip("\n") + "\n"
    if role not in {"developer", "tester", "fixer"}:
        raise PipelineError("read-only role returned a patch")
    if any(
        line.startswith(
            (
                "rename ",
                "copy ",
                "GIT binary",
                "old mode",
                "new mode",
                "new file mode 120",
                "deleted file mode 120",
            )
        )
        for line in patch.splitlines()
    ):
        raise PipelineError("renames, symlinks, binary and mode changes are not allowed")
    paths = []
    for line in patch.splitlines():
        if line.startswith(("--- ", "+++ ")):
            value = line[4:]
            if value == "/dev/null":
                if line.startswith("+++ "):
                    raise PipelineError("automatic file deletion is not allowed")
                continue
            if not value.startswith(("a/", "b/")) or any(c.isspace() for c in value):
                raise PipelineError("unsupported patch path")
            name = value[2:]
            path = contained(worktree, name)
            if path.name.startswith(".env") or path.suffix in {".pem", ".key"}:
                raise PipelineError("secret file patch denied")
            is_test = in_paths(name, test_paths)
            if role == "tester" and not is_test:
                raise PipelineError("test agent attempted production change")
            if role in {"developer", "fixer"} and is_test and path.exists():
                raise PipelineError("existing tests are protected from developer/fixer changes")
            if name not in paths:
                paths.append(name)
    if not paths:
        raise PipelineError("patch contains no validated file headers")
    git = Git(worktree)
    git.call("apply", "--recount", "--check", "--whitespace=error", "-", input=patch)
    # Validate Git's own interpretation as well; fake headers cannot authorize extra files.
    stats = git.call("apply", "--recount", "--numstat", "-", input=patch)
    actual = [line.split("\t", 2)[2] for line in stats.splitlines()]
    if set(actual) != set(paths):
        raise PipelineError("patch path interpretation mismatch")
    git.call("apply", "--recount", "--whitespace=error", "-", input=patch)
    return paths
