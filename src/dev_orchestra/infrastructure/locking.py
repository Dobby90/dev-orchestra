"""OS locks complement persisted state; a stale database row never grants ownership."""

import fcntl
import time
from contextlib import contextmanager
from pathlib import Path

from dev_orchestra.domain.models import PipelineError, StopRequested


class OwnershipError(PipelineError):
    pass


@contextmanager
def lock(path: Path, wait=False, stop=lambda: None):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        while True:
            if stop():
                raise StopRequested(stop())
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if not wait:
                    raise OwnershipError("execution is already owned by another worker") from error
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
