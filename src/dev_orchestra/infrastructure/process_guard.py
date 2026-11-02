"""Command guardian: owner-pipe EOF cleans up even after Worker SIGKILL."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.process_owner import alive, identity


def live(process):
    try:
        return process.is_running() and process.status() not in {
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        }
    except psutil.NoSuchProcess:
        return False


def main():
    fd, primary, mirror, token, owner_json, *argv = sys.argv[1:]
    fd = int(fd)
    primary, mirror = Path(primary), Path(mirror)
    os.set_blocking(fd, False)
    control = primary.with_name(token + ".control.json")
    record = {
        "protocol": 2,
        "token": token,
        "finished": False,
        "termination_confirmed": False,
        "guardian": identity(os.getpid()),
    }
    child = None
    tracked = {}
    reason = "completed"

    def save():
        write(primary, record)
        if mirror != primary:
            write(mirror, record)

    def remember():
        if child is None:
            return
        roots = [psutil.Process(child.pid), *list(tracked.values())]
        for root in roots:
            try:
                for process in [root, *root.children(recursive=True)]:
                    tracked[(process.pid, process.create_time())] = process
            except psutil.NoSuchProcess:
                pass

    def group_members():
        # Keep the direct child unreaped until group cleanup pins its group ID.
        for process in psutil.process_iter(["pid"]):
            try:
                if os.getpgid(process.pid) == child.pid:
                    process = psutil.Process(process.pid)
                    tracked[(process.pid, process.create_time())] = process
            except (ProcessLookupError, psutil.NoSuchProcess):
                pass

    def signal_owned(sig):
        # psutil checks PID reuse before signalling each captured process.
        for process in list(tracked.values()):
            try:
                if live(process):
                    process.send_signal(sig)
            except psutil.NoSuchProcess:
                pass

    try:
        record["owner"] = json.loads(owner_json)
        save()  # Registers preflight commands as well as role commands.
        try:
            if os.read(fd, 1) == b"":
                raise RuntimeError("execution owner disappeared before startup")
        except BlockingIOError:
            pass
        if not alive(record["owner"]):
            raise RuntimeError("execution owner disappeared before startup")
        child = subprocess.Popen(
            argv, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, start_new_session=True
        )
        record.update(identity(child.pid))
        record["pgid"] = child.pid
        remember()
        save()
        while True:
            remember()
            try:
                if os.read(fd, 1) == b"":
                    reason = "owner_lost"
                    break
            except BlockingIOError:
                pass
            if control.exists():
                request = json.loads(control.read_text())
                if request == {"token": token, "action": "stop"}:
                    reason = "stop_requested"
                    break
            if not alive(record):
                break
            time.sleep(0.03)
    except BaseException as error:
        reason = "guardian_error"
        record["error"] = type(error).__name__
        record["error_message"] = str(error)
    finally:
        confirmed = True
        if child is not None:
            try:
                remember()
                group_members()
                signal_owned(signal.SIGTERM)
                deadline = time.monotonic() + 0.4
                while time.monotonic() < deadline and any(live(p) for p in tracked.values()):
                    remember()
                    time.sleep(0.02)
                group_members()
                signal_owned(signal.SIGKILL)
                child.wait(timeout=3)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and any(live(p) for p in tracked.values()):
                    signal_owned(signal.SIGKILL)
                    time.sleep(0.02)
                confirmed = not any(live(p) for p in tracked.values())
            except (psutil.Error, OSError, subprocess.TimeoutExpired):
                confirmed = False
        record.update(finished=confirmed, termination_confirmed=confirmed, reason=reason)
        record["tracked"] = [{"pid": pid, "create_time": birth} for pid, birth in tracked]
        save()
        os.close(fd)
    if not confirmed or child is None:
        return 125
    return child.returncode if child.returncode >= 0 else 128 - child.returncode


if __name__ == "__main__":
    raise SystemExit(main())
