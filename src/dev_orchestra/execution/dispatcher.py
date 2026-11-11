"""SQLite queue consumer. API restarts never own this process's lifetime."""

import os
import signal
import sys
import time
from pathlib import Path

from dev_orchestra.application.execution import ExecutionService
from dev_orchestra.application.queue import QueueService
from dev_orchestra.application.webhooks import WebhookService
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.infrastructure.log_index import IndexBudget, LogIndexer
from dev_orchestra.infrastructure.process_owner import identity
from dev_orchestra.infrastructure.store import Store


class Dispatcher:
    def __init__(self, store):
        self.store, self.queue = store, QueueService(store)
        self.stopped = False
        self.log_cursor = 0
        self.active_cursor = 0

    def tick(self):
        self.queue.recover()
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
        # Queue admission always precedes bounded observability work.
        self.index_logs()
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break
            except ChildProcessError:
                break

    def index_logs(self):
        budget = IndexBudget(files=32, deadline=time.monotonic() + 0.05)
        with self.store.connection() as db:
            active = db.execute(
                "SELECT rowid,run_id AS id FROM jobs WHERE state IN ('claimed','running') "
                "ORDER BY (rowid<=?),rowid LIMIT 8",
                (self.active_cursor,),
            ).fetchall()
            history = db.execute(
                "SELECT rowid,id FROM runs WHERE rowid>? AND NOT EXISTS "
                "(SELECT 1 FROM jobs WHERE jobs.run_id=runs.id AND state IN ('claimed','running')) "
                "ORDER BY rowid LIMIT 4",
                (self.log_cursor,),
            ).fetchall()
        if not history:
            self.log_cursor = 0
        for row, closed in [(r, False) for r in active] + [(r, True) for r in history]:
            if budget.files <= 0 or time.monotonic() >= budget.deadline:
                break
            if closed:
                self.log_cursor = row["rowid"]
            else:
                self.active_cursor = row["rowid"]
            try:
                LogIndexer(self.store).index(
                    row["id"], closed, budget=budget, rescan_interval=30 if closed else 0
                )
            except OwnershipError:
                pass
            except Exception as error:
                from dev_orchestra.infrastructure.process import redact

                print(redact(str(error)), file=sys.stderr, flush=True)

    def run(self):
        if not self.queue.enabled():
            raise RuntimeError("run database-upgrade before starting Dispatcher")
        with (
            lock(self.store.path.with_suffix(".dispatcher.lock")),
            lock(self.store.path.with_suffix(".maintenance.lock"), shared=True),
            WebhookService(self.store).worker(),
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
