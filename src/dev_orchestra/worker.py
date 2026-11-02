"""Private process entrypoint; a future worker command can reuse the runner."""
import sys
from pathlib import Path
from dev_orchestra.config import Config
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.infrastructure.store import Store
from dev_orchestra.infrastructure.test_runner import TestRunner
from dev_orchestra.orchestration.runner import Runner


def main():
    store = Store(Path(sys.argv[1]))
    run_id = sys.argv[2]
    config = Config.model_validate(store.get(run_id)["config"])
    client = CodexClient(config.codex)
    try:
        client.check()
        Runner(store, client, TestRunner()).execute(run_id)
    except Exception as error:
        from dev_orchestra.infrastructure.process import redact
        store.update(run_id, "failed", error=redact(str(error)))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
