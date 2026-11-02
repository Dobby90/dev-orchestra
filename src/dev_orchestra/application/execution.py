"""Transport-independent execution application service."""

import subprocess
import sys
from pathlib import Path

import psutil

from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.process_owner import identity


class ExecutionService:
    def __init__(self, store, reconcile):
        self.store, self.status = store, reconcile

    def start(self, run_id, detached=False):
        args = [sys.executable, "-m", "dev_orchestra.worker", str(self.store.path), run_id]
        directory = Path(self.store.get(run_id)["directory"])
        with (directory / "worker.log").open("a") as output:
            process = subprocess.Popen(
                args, stdout=output, stderr=subprocess.STDOUT, start_new_session=True
            )
        try:
            launch = identity(process.pid)
        except psutil.NoSuchProcess:
            launch = {"pid": process.pid, "create_time": None}
        write(directory / "launch.json", launch)
        if not detached:
            try:
                process.wait()
            except KeyboardInterrupt:
                self.store.control(run_id, "pause")
                process.wait()
            self.status(run_id)
        return process.pid
