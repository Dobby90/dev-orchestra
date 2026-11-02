"""Atomic artifacts and boundary-safe paths."""

import hashlib
import json
import os
import tempfile
from pathlib import Path, PurePosixPath

from dev_orchestra.domain.models import PipelineError


def private_append(path: Path):
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def relative_path(value: str) -> str:
    p = PurePosixPath(value)
    if (
        not value
        or p.is_absolute()
        or any(x in ("..", ".git", ".codex", ".agents", ".aws") for x in p.parts)
        or str(p) == "."
        or "\\" in value
    ):
        raise PipelineError(f"unsafe relative path: {value}")
    return str(p)


def contained(root: Path, value: str) -> Path:
    root = root.resolve()
    candidate = root / relative_path(value)
    if not candidate.resolve().is_relative_to(root):
        raise PipelineError("path escapes workspace")
    current = candidate
    while current != root:
        if current.is_symlink():
            raise PipelineError("symlink paths are not allowed")
        current = current.parent
    return candidate


def write(path: Path, value) -> str:
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    temp = Path(filename)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)
    return digest(data.encode("utf-8"))
