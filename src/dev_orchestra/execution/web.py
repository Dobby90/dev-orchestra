"""Own only API/Dispatcher children; normal GUI shutdown preserves Run Workers."""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time

from dev_orchestra.application.queue import QueueService
from dev_orchestra.application.service import RunService
from dev_orchestra.config import Config
from dev_orchestra.execution.lifetime import parent_channel
from dev_orchestra.infrastructure.files import private_append, write
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process import redact
from dev_orchestra.infrastructure.process_owner import identity


def run():
    value = json.loads(sys.stdin.read())
    service = RunService(Config.model_validate(value["config"]))
    db = service.store.path
    marker = db.with_suffix(".web-runtime.json")
    control = db.with_suffix(".web-stop.json")
    stopped = False

    def stop(*args):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    children = []
    record = None
    with (
        lock(db.with_suffix(".web.lock")),
        socket.socket() as listener,
        parent_channel() as owner_fd,
    ):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", value["port"]))
        listener.listen(128)
        config_path = db.with_suffix(".web-config.json")
        write(config_path, value)
        with private_append(db.with_suffix(".web.log")) as output:
            try:
                api = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "dev_orchestra.execution.web_server",
                        str(config_path),
                        str(listener.fileno()),
                        str(owner_fd),
                    ],
                    pass_fds=[listener.fileno(), owner_fd],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                children.append(api)
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if stopped:
                        raise RuntimeError("web startup interrupted")
                    if api.poll() is not None:
                        raise RuntimeError("API child failed")
                    connection = http.client.HTTPConnection("127.0.0.1", value["port"], timeout=0.3)
                    try:
                        connection.request("GET", "/api/health")
                        if connection.getresponse().status == 200:
                            break
                    except OSError:
                        time.sleep(0.05)
                    finally:
                        connection.close()
                else:
                    raise RuntimeError("API startup timeout")
                reused = QueueService(service.store).dispatcher_status()["alive"]
                if stopped:
                    raise RuntimeError("web startup interrupted")
                if not reused:
                    dispatcher = subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "dev_orchestra.execution.dispatcher",
                            str(db),
                            str(owner_fd),
                        ],
                        pass_fds=[owner_fd],
                        stdin=subprocess.DEVNULL,
                        stdout=output,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                    children.append(dispatcher)
                    deadline = time.monotonic() + 5
                    while (
                        time.monotonic() < deadline
                        and not QueueService(service.store).dispatcher_status()["alive"]
                    ):
                        if stopped:
                            raise RuntimeError("web startup interrupted")
                        if dispatcher.poll() is not None:
                            raise RuntimeError("Dispatcher child failed")
                        time.sleep(0.05)
                    if not QueueService(service.store).dispatcher_status()["alive"]:
                        raise RuntimeError("Dispatcher startup timeout")
                record = identity(os.getpid()) | {
                    "token": value["token"],
                    "port": value["port"],
                    "dispatcher_reused": reused,
                }
                write(marker, record)
                while not stopped:
                    if any(child.poll() is not None for child in children):
                        raise RuntimeError("owned web child stopped")
                    if control.exists():
                        request = json.loads(control.read_text())
                        if request.get("token") == value["token"]:
                            if request.get("cancel_active"):
                                for item in service.store.list():
                                    if item["state"] not in {"completed", "cancelled"}:
                                        service.cancel(item["id"])
                            break
                    time.sleep(0.2)
            finally:
                for child in reversed(children):
                    if child.poll() is None:
                        child.terminate()
                for child in reversed(children):
                    try:
                        child.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                if (
                    record
                    and marker.exists()
                    and json.loads(marker.read_text()).get("token") == value["token"]
                ):
                    write(marker, record | {"finished": True})


if __name__ == "__main__":
    try:
        run()
    except Exception as error:
        print(redact(str(error)), file=sys.stderr, flush=True)
        raise SystemExit(1)
