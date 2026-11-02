"""OS locks complement persisted state; a stale database row never grants ownership."""

import fcntl
from contextlib import contextmanager
from pathlib import Path

from dev_orchestra.domain.models import PipelineError


class OwnershipError(PipelineError):
    pass


@contextmanager
def lock(path: Path, wait=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise OwnershipError("execution is already owned by another worker") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
