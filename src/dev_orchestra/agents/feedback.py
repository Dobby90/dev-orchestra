"""Bounded, read-only source context for rejected patches."""

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained
from dev_orchestra.infrastructure.git import Git


def source_context(worktree, patch):
    tracked = set(Git(worktree).call("ls-files", "-z").split("\0"))
    result = []
    remaining = 16384
    for line in patch.splitlines():
        if not line.startswith(("--- a/", "+++ b/")):
            continue
        name = line[6:]
        if name not in tracked or any(x["path"] == name for x in result):
            continue
        try:
            path = contained(worktree, name)
            if any(
                part.startswith(".env") for part in path.relative_to(worktree).parts
            ) or path.suffix in {".key", ".pem"}:
                continue
            if not path.is_file() or path.stat().st_size > remaining:
                continue
            with path.open("rb") as handle:
                raw = handle.read(remaining + 1)
            if len(raw) > remaining:
                continue
            text = raw.decode("utf-8")
        except (PipelineError, OSError, UnicodeError):
            continue
        remaining -= len(text.encode())
        result.append({"path": name, "content": text})
        if len(result) == 4:
            break
    return result
