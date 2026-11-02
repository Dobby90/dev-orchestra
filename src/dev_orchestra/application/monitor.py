"""Worker-owned heartbeat and log indexing, independent of API/Dispatcher."""

import threading

from dev_orchestra.infrastructure.locking import OwnershipError
from dev_orchestra.infrastructure.log_index import LogIndexer
from dev_orchestra.infrastructure.process import redact


class WorkerMonitor:
    def __init__(self, queue, job_id, token, run_id):
        self.queue, self.job_id, self.token, self.run_id = queue, job_id, token, run_id
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def poll(self, closed=False):
        self.queue.heartbeat(self.job_id, self.token)
        try:
            return LogIndexer(self.queue.store).index(self.run_id, closed)
        except OwnershipError:
            pass

    def run(self):
        while not self.stop_event.wait(0.5):
            try:
                self.poll()
            except Exception as error:
                # Observability failure must not pretend the command was cancelled.
                self.last_error = redact(str(error))

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        self.thread.join(timeout=12)
        try:
            while self.poll(closed=True):
                pass  # Drain the bounded indexing backlog before the final ownership commit.
        except Exception as error:
            self.last_error = redact(str(error))
        if getattr(self, "last_error", None):
            with self.queue.store.connection() as db:
                db.execute(
                    "UPDATE jobs SET error=? WHERE id=? AND claim_token=?",
                    (self.last_error[:2000], self.job_id, self.token),
                )
