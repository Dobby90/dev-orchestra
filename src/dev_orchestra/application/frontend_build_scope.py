"""Identify changes that can affect a standalone application's bundler."""

import re
from pathlib import Path, PurePosixPath

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.git import Git

COMPONENT_DIRECTORIES = {("src", "components"), ("src", "views")}
COMPONENT_SUFFIXES = {".vue", ".js", ".ts", ".jsx", ".tsx"}
BUNDLE_REFERENCE = re.compile(
    r"^\s*(?:import|export)\b|\b(?:import|require(?:\.resolve)?|url)\s*\(|\bnew\s+URL\s*\("
    r"""|\bfrom\s*['"]|['"`](?:\.{1,2}/|@/|~/)|\bimport\.meta\b"""
    r"|\b(?:src|srcset)\s*=|\bimage-set\s*\("
    r"|^\s*(?:lang|module|scoped)(?:\s*=|\s*/?>?\s*$)"
    r"|@(?:import|use|forward)\b"
)
VUE_BLOCK = re.compile(r"</?(?:script|style|template)\b[^>]*>")


def frontend_bundle_changed(run, candidate, directory, paths):
    """Keep the build for entry points, configuration and uncertain component edits."""
    component_paths = []
    for name in paths:
        path = PurePosixPath(name).relative_to(directory)
        if path.parts[:1] == ("tests",):
            continue
        if path.parts[:2] not in COMPONENT_DIRECTORIES or path.suffix not in COMPONENT_SUFFIXES:
            return True
        component_paths.append(name)
    if not component_paths:
        return False
    try:
        diff = Git(Path(run["repo"])).call(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-renames",
            "--unified=0",
            run["base_sha"],
            candidate,
            "--",
            *(f":(literal){name}" for name in component_paths),
        )
    except PipelineError:
        return True
    if not diff:
        return True
    blocks = {"+": [], "-": []}
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            if blocks["+"] != blocks["-"]:
                return True
            blocks = {"+": [], "-": []}
        if line.startswith(("new file mode ", "deleted file mode ", "old mode ", "Binary files ")):
            return True
        if line.startswith("index ") and line.endswith(" 120000"):
            return True
        if line[:1] in blocks and not line.startswith(("+++ ", "--- ")):
            if BUNDLE_REFERENCE.search(line[1:]):
                return True
            blocks[line[0]].extend(VUE_BLOCK.findall(line[1:]))
    return blocks["+"] != blocks["-"]
