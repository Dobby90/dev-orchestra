"""Persistent process ownership; never signal a PID recovered from disk."""

import json
import re
import time

import psutil

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import write


def identity(pid):
    process = psutil.Process(pid)
    return {"pid": pid, "create_time": process.create_time()}


def alive(record):
    """Birth time prevents confusing a reused PID with an execution owner."""
    if not record or not record.get("create_time"):
        return False
    try:
        process = psutil.Process(record["pid"])
        return process.create_time() == record["create_time"] and process.status() not in {
            psutil.STATUS_ZOMBIE,
            psutil.STATUS_DEAD,
        }
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except psutil.AccessDenied as error:
        raise PipelineError("cannot verify process ownership") from error


def markers(directory):
    paths = list((directory / "processes").glob("*.json"))
    # Legacy stage markers remain readable, but cannot authorize PID-based signals.
    paths += list(directory.glob("iterations/*/*/*/*.process.json"))
    seen = set()
    for path in paths:
        if path.name.endswith(".control.json"):
            continue
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError) as error:
            raise PipelineError("invalid process ownership record") from error
        key = record.get("token", str(path))
        if key not in seen:
            seen.add(key)
            yield path, record


def ensure_stopped(directory, timeout=5):
    """Ask a live authenticated guardian to stop; fail closed without ownership."""
    pending = []
    for path, record in markers(directory):
        if record.get("finished"):
            if record.get("protocol") == 2 and not record.get("termination_confirmed"):
                raise PipelineError("process termination is not confirmed")
            continue
        if record.get("protocol") != 2:
            raise PipelineError("legacy orphan needs manual ownership verification")
        if not re.fullmatch(r"[0-9a-f]{32}", record.get("token", "")):
            raise PipelineError("invalid process ownership token")
        if not alive(record.get("guardian")):
            raise PipelineError("process guardian unavailable; manual intervention required")
        control = path.with_name(record["token"] + ".control.json")
        # The guardian reads only its own private control path and nonce.
        write(control, {"token": record["token"], "action": "stop"})
        pending.append(path)
    deadline = time.monotonic() + timeout
    for path in pending:
        while time.monotonic() < deadline:
            record = json.loads(path.read_text())
            if record.get("finished") and record.get("termination_confirmed"):
                break
            time.sleep(0.03)
        else:
            raise PipelineError("process termination is not confirmed")
