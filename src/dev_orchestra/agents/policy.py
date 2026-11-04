"""Host validation of model-supplied patches before any source mutation."""

import re
from pathlib import Path

from dev_orchestra.agents.contracts import ResponseError
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained, digest
from dev_orchestra.infrastructure.git import Git


def in_paths(path, roots):
    return any(path == root or path.startswith(root.rstrip("/") + "/") for root in roots)


class TestChangesRequired(PipelineError):
    def __init__(self, paths):
        self.paths = paths
        super().__init__(
            "existing tests are protected; requirement-bound test change evidence required: "
            + ", ".join(paths)
        )


def validate_patch(
    worktree: Path,
    patch: str,
    role: str,
    test_paths: list[str],
    *,
    approved_patch_hash=None,
    validated_patch_hash=None,
):
    if not patch:
        return "", []
    if approved_patch_hash is not None and digest(patch.encode()) != approved_patch_hash:
        raise PipelineError("approved patch checksum mismatch")
    if validated_patch_hash is not None and digest(patch.encode()) != validated_patch_hash:
        raise PipelineError("validated patch checksum mismatch")
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
    if any(line.startswith("*** ") for line in patch.splitlines()):
        raise ResponseError("use Git unified diff headers, never *** patch markers")
    paths, protected = [], []
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
            if is_test and path.exists() and name not in protected:
                protected.append(name)
            if name not in paths:
                paths.append(name)
    if not paths:
        raise PipelineError("patch contains no validated file headers")
    git = Git(worktree)
    try:
        git.call("apply", "--recount", "--check", "--whitespace=error", "-", input=patch)
    except PipelineError as error:
        raise ResponseError(
            "patch check failed; read current files and regenerate exact unified diff: "
            + str(error)[:2000]
        ) from error
    # Validate Git's own interpretation as well; fake headers cannot authorize extra files.
    stats = git.call("apply", "--recount", "--numstat", "-", input=patch)
    actual = [line.split("\t", 2)[2] for line in stats.splitlines()]
    if set(actual) != set(paths):
        raise PipelineError("patch path interpretation mismatch")
    if protected and approved_patch_hash is None and validated_patch_hash is None:
        raise TestChangesRequired(protected)
    return patch, paths


def apply_patch(
    worktree: Path,
    patch: str,
    role: str,
    test_paths: list[str],
    *,
    approved_patch_hash=None,
    validated_patch_hash=None,
):
    patch, paths = validate_patch(
        worktree,
        patch,
        role,
        test_paths,
        approved_patch_hash=approved_patch_hash,
        validated_patch_hash=validated_patch_hash,
    )
    if patch:
        Git(worktree).call("apply", "--recount", "--whitespace=error", "-", input=patch)
    return paths
