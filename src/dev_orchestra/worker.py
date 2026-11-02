"""Private process entrypoint; a future worker command can reuse the runner."""

import json
import sys
from contextlib import nullcontext
from pathlib import Path

from dev_orchestra.config import Config
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.infrastructure.locking import OwnershipError
from dev_orchestra.infrastructure.store import Store
from dev_orchestra.infrastructure.test_runner import TestRunner
from dev_orchestra.orchestration.runner import Runner


def execute():
    store = Store(Path(sys.argv[1]))
    run_id = sys.argv[2]
    config = Config.model_validate(store.get(run_id)["config"])
    client = CodexClient(config.codex)
    claim = None
    if len(sys.argv) > 3:
        from dev_orchestra.application.monitor import WorkerMonitor
        from dev_orchestra.application.queue import QueueService

        token = json.loads(sys.stdin.read())["token"]
        claim = (sys.argv[3], token)
        queue = QueueService(store)
        monitor = WorkerMonitor(queue, *claim, run_id)
    else:
        monitor = nullcontext()
    try:
        with monitor:
            Runner(store, client, TestRunner()).execute(run_id, claim=claim)
    except OwnershipError:
        raise SystemExit(2)
    except Exception as error:
        from dev_orchestra.infrastructure.process import redact

        store.update(run_id, "failed", error=redact(str(error)))
        Runner(store, None, None).report(run_id)
        raise SystemExit(1)
    finally:
        if claim:
            queue.finish(*claim)


def main():
    from dev_orchestra.infrastructure.locking import lock

    with lock(Path(sys.argv[1]).with_suffix(".maintenance.lock"), shared=True):
        execute()


if __name__ == "__main__":
    main()
