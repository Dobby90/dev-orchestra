"""SQLite queue consumer. API restarts never own this process's lifetime."""

import os
import signal
import sys
import time
from pathlib import Path

from dev_orchestra.application.execution import ExecutionService
from dev_orchestra.application.queue import QueueService
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.infrastructure.log_index import LogIndexer
from dev_orchestra.infrastructure.process_owner import alive, identity
from dev_orchestra.infrastructure.store import Store


class Dispatcher:
    def __init__(self, store):
        self.store, self.queue = store, QueueService(store)
        self.stopped = False

    def tick(self):
        self.queue.recover()
        with self.store.connection() as db:
            rows = db.execute("SELECT id FROM runs ORDER BY rowid").fetchall()
        for row in rows:
            run = self.store.get(row["id"])
            # Closed only when no execution owner is alive; no UTF-8 partial tail reads.
            with self.store.connection() as db:
                job = db.execute(
                    "SELECT owner_pid,owner_create_time FROM jobs WHERE run_id=? AND state IN ('claimed','running')",
                    (run["id"],),
                ).fetchone()
            closed = not job or not alive(
                {"pid": job["owner_pid"], "create_time": job["owner_create_time"]}
            )
            try:
                LogIndexer(self.store).index(run["id"], closed)
            except OwnershipError:
                pass
            except Exception as error:
                from dev_orchestra.infrastructure.process import redact

                print(redact(str(error)), file=sys.stderr, flush=True)
        if self.stopped:
            return
        with self.store.connection() as db:
            stopped = db.execute("SELECT stop_requested FROM dispatchers WHERE slot=1").fetchone()
            if stopped and stopped[0]:
                return
        job = self.queue.reserve()
        if job:
            from dev_orchestra.application.service import RunService
            from dev_orchestra.config import Config

            config = Config.model_validate(self.store.get(job["run_id"])["config"])
            # Launch uses the common code path and never blocks for a full pipeline.
            ExecutionService(self.store, RunService(config).status).launch(job["run_id"], True, job)
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break
            except ChildProcessError:
                break

    def run(self):
        if not self.queue.enabled():
            raise RuntimeError("run database-upgrade before starting Dispatcher")
        with (
            lock(self.store.path.with_suffix(".dispatcher.lock")),
            lock(self.store.path.with_suffix(".maintenance.lock"), shared=True),
        ):
            owner = identity(os.getpid())
            with self.store.connection() as db:
                db.execute(
                    "INSERT OR REPLACE INTO dispatchers VALUES(1,?,?,?,0)",
                    (owner["pid"], owner["create_time"], time.time()),
                )

            def stop(*args):
                self.stopped = True

            previous = {sig: signal.signal(sig, stop) for sig in [signal.SIGTERM, signal.SIGINT]}
            try:
                while not self.stopped:
                    with self.store.connection() as db:
                        row = db.execute(
                            "SELECT stop_requested FROM dispatchers WHERE slot=1"
                        ).fetchone()
                        if row and row[0]:
                            break
                        db.execute(
                            "UPDATE dispatchers SET heartbeat_at=? WHERE slot=1 AND pid=?",
                            (time.time(), os.getpid()),
                        )
                    try:
                        self.tick()
                    except Exception as error:
                        from dev_orchestra.infrastructure.process import redact

                        print(redact(str(error)), file=sys.stderr, flush=True)
                    time.sleep(0.3)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                with self.store.connection() as db:
                    db.execute(
                        "DELETE FROM dispatchers WHERE slot=1 AND pid=? AND create_time=?",
                        (owner["pid"], owner["create_time"]),
                    )


def main():
    try:
        dispatcher = Dispatcher(Store(Path(sys.argv[1])))
        if len(sys.argv) > 2:
            from dev_orchestra.execution.lifetime import watch_parent

            watch_parent(int(sys.argv[2]), lambda: setattr(dispatcher, "stopped", True))
        dispatcher.run()
    except OwnershipError:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
