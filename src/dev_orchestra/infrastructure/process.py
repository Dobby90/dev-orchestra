"""Bounded subprocess execution and secret-redacted output."""
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from dev_orchestra.domain.models import PipelineError, StopRequested


def redact(text):
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)([\s:=]+)[^\s,\"']+", r"\1\2[REDACTED]", text)
    return re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)


def environment():
    return {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT", "SSL_CERT_FILE"}} | {"PYTHONDONTWRITEBYTECODE": "1"}


def execute(argv, cwd: Path, timeout, log: Path, stdin=None, stop=lambda: None, env=None):
    log.parent.mkdir(parents=True, exist_ok=True)
    # communicate drains both pipes without deadlocking; periodic calls enable cancellation.
    with subprocess.Popen(argv, cwd=cwd, env=env or environment(), stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, start_new_session=True) as process:
        deadline = time.monotonic() + timeout
        pending = stdin
        try:
            while True:
                control = stop()
                if control:
                    raise StopRequested(control)
                if time.monotonic() >= deadline:
                    raise PipelineError("process timeout")
                try:
                    output, _ = process.communicate(pending, timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pending = None
        except BaseException:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                output, _ = process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                output, _ = process.communicate()
            log.write_text(redact(output))
            raise
        log.write_text(redact(output))
        return process.returncode, redact(output)
