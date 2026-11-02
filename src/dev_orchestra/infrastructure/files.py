"""Atomic artifacts and boundary-safe paths."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from dev_orchestra.domain.models import PipelineError


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def relative_path(value: str) -> str:
    p = PurePosixPath(value)
    if not value or p.is_absolute() or any(x in ("..", ".git", ".codex", ".agents", ".aws") for x in p.parts) or str(p) == "." or "\\" in value:
        raise PipelineError(f"unsafe relative path: {value}")
    return str(p)


def contained(root: Path, value: str) -> Path:
    candidate = root / relative_path(value)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise PipelineError("path escapes workspace")
    if any(p.is_symlink() for p in [candidate, *candidate.parents] if p != root.parent):
        raise PipelineError("symlink paths are not allowed")
    return candidate


def write(path: Path, value) -> str:
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)
    return digest(path.read_bytes())
