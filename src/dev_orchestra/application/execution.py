"""Transport-independent execution application service."""

import json
import subprocess
import sys
import time
from pathlib import Path

import psutil

from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.process_owner import identity


class ExecutionService:
    def __init__(self, store, reconcile):
        self.store, self.status = store, reconcile

    def start(self, run_id, detached=False):
        from dev_orchestra.application.queue import QueueService
        from dev_orchestra.infrastructure.locking import OwnershipError
        from dev_orchestra.infrastructure.process_owner import alive

        queue = QueueService(self.store)
        job = None
        if queue.enabled():
            job = queue.reserve(run_id)
            if not job.get("_launch"):
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        existing = queue.job(job["id"])
                        if existing["state"] == "queued" or (
                            existing["state"] in {"claimed", "running"}
                            and existing["claim_token"] != job["claim_token"]
                        ):
                            raise OwnershipError(
                                "worker execution attempt changed; retry explicitly"
                            )
                        if alive(
                            {
                                "pid": existing["owner_pid"],
                                "create_time": existing["owner_create_time"],
                            }
                        ):
                            if not detached:
                                self.wait_existing(run_id, existing)
                            return existing["owner_pid"]
                        if existing["state"] not in {"claimed", "running"}:
                            if not detached:
                                self.status(run_id)
                            return existing["owner_pid"]
                        if existing["owner_pid"] and not detached:
                            self.wait_existing(run_id, existing)
                            return existing["owner_pid"]
                        time.sleep(0.03)
                    except KeyboardInterrupt:
                        self.store.control(run_id, "pause")
                raise OwnershipError("worker launch pending or owner lost; inspect before retry")
        return self.launch(run_id, detached, job)

    def wait_existing(self, run_id, job):
        from dev_orchestra.application.queue import QueueService
        from dev_orchestra.infrastructure.locking import OwnershipError
        from dev_orchestra.infrastructure.process_owner import alive

        queue = QueueService(self.store)
        while True:
            try:
                current = queue.job(job["id"])
                if current["state"] not in {"claimed", "running"}:
                    if current["state"] == "queued":
                        raise OwnershipError(
                            "worker execution attempt was requeued; retry explicitly"
                        )
                    break
                if current["claim_token"] != job["claim_token"]:
                    raise OwnershipError("worker execution attempt changed; inspect before retry")
                owner = {"pid": current["owner_pid"], "create_time": current["owner_create_time"]}
                if (owner["pid"], owner["create_time"]) != (
                    job["owner_pid"],
                    job["owner_create_time"],
                ):
                    raise OwnershipError("worker owner changed; inspect before retry")
                if not alive(owner):
                    queue.recover()
                time.sleep(0.03)
            except KeyboardInterrupt:
                self.store.control(run_id, "pause")
        self.status(run_id)

    def launch(self, run_id, detached=False, job=None):
        from dev_orchestra.application.queue import QueueService

        args = [sys.executable, "-m", "dev_orchestra.worker", str(self.store.path), run_id]
        if job:
            args.append(job["id"])
        directory = Path(self.store.get(run_id)["directory"])
        with (directory / "worker.log").open("a") as output:
            process = subprocess.Popen(
                args,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                stdin=subprocess.PIPE if job else subprocess.DEVNULL,
            )
        if job:
            try:
                process.stdin.write(json.dumps({"token": job["claim_token"]}).encode())
                process.stdin.close()
            except BrokenPipeError:
                pass
        try:
            launch = identity(process.pid)
        except psutil.NoSuchProcess:
            launch = {"pid": process.pid, "create_time": None}
        write(directory / "launch.json", launch)
        if job and launch["create_time"] is not None:
            QueueService(self.store).launched(job["id"], job["claim_token"], launch)
        if not detached:
            try:
                process.wait()
            except KeyboardInterrupt:
                self.store.control(run_id, "pause")
                process.wait()
            self.status(run_id)
        return process.pid
