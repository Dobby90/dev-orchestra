"""Local GUI supervisor control. Disk PID records never authorize OS signals."""

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import private_append, write
from dev_orchestra.infrastructure.process_owner import alive


class WebRuntime:
    def __init__(self, service):
        self.service = service
        self.path = service.store.path.with_suffix(".web-runtime.json")

    def status(self):
        if not self.path.exists():
            return {"alive": False}
        record = json.loads(self.path.read_text())
        return {
            "alive": not record.get("finished") and alive(record),
            "pid": record["pid"],
            "create_time": record["create_time"],
            "port": record["port"],
            "dispatcher_reused": record.get("dispatcher_reused", False),
            "error": record.get("error"),
        }

    def start(self, roots, port=8000, detached=False, assets=None):
        try:
            from dev_orchestra.api.static import bundle
        except ImportError as error:
            raise PipelineError("install the web extra before web start") from error
        bundle(assets, required=True)
        with self.service.store.connection() as db:
            if db.execute("SELECT version FROM schema_version").fetchone()[0] not in {5, 6}:
                raise PipelineError("run database-upgrade first")
        if self.status()["alive"]:
            raise PipelineError("web service is already running")
        token = uuid.uuid4().hex
        output = self.service.store.path.with_suffix(".web.log")
        with private_append(output) as log:
            child = subprocess.Popen(
                [sys.executable, "-m", "dev_orchestra.execution.web", str(self.service.store.path)],
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        try:
            child.stdin.write(
                json.dumps(
                    {
                        "token": token,
                        "config": self.service.config.model_dump(mode="json"),
                        "roots": [str(Path(p).expanduser().resolve()) for p in roots],
                        "port": port,
                        "assets": str(Path(assets).resolve()) if assets else None,
                    }
                ).encode()
            )
            child.stdin.close()
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                state = self.status()
                if (
                    state["alive"]
                    and self.path.exists()
                    and json.loads(self.path.read_text()).get("token") == token
                ):
                    break
                if child.poll() is not None:
                    raise PipelineError("web service failed to start; inspect the local .web.log")
                time.sleep(0.05)
            else:
                raise PipelineError("web startup not confirmed; inspect web status")
            if not detached:
                try:
                    child.wait()
                except KeyboardInterrupt:
                    self.stop()
                    child.wait()
            return state if detached else self.status()
        except BaseException:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            raise

    def stop(self, cancel_active=False):
        if not self.status()["alive"]:
            return {"alive": False}
        record = json.loads(self.path.read_text())
        write(
            self.service.store.path.with_suffix(".web-stop.json"),
            {"token": record["token"], "cancel_active": cancel_active},
        )
        return {"stop_requested": True, "active_workers_preserved": not cancel_active}
