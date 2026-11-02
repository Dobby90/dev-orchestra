"""Bounded, streaming subprocess execution and secret-redacted output."""

import codecs
import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure.files import write


def redact(text):
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", text)
    text = re.sub(
        r'(?i)(["\']?(?:api[_-]?key|token|password|secret|authorization)["\']?\s*[:=]\s*)(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,}]+)',
        r'\1"[REDACTED]"',
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", text)
    return re.sub(r"\b(?:sk-|github_pat_|ghp_)[A-Za-z0-9_-]+", "[REDACTED]", text)


def environment():
    return {
        k: v
        for k, v in os.environ.items()
        if k in {"PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT", "SSL_CERT_FILE"}
    } | {"PYTHONDONTWRITEBYTECODE": "1"}


def execute(argv, cwd: Path, timeout, log: Path, stdin=None, stop=lambda: None, env=None):
    log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as input_file, log.open("w") as output_file:
        input_file.write((stdin or "").encode())
        input_file.seek(0)
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env or environment(),
            stdin=input_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        marker = log.with_suffix(".process.json")
        write(marker, {"pid": process.pid, "finished": False})
        deadline = time.monotonic() + timeout
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        pending, collected, total = "", [], 0

        def emit(line):
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get("item", {}).get("type") == "reasoning":
                    return
            except (ValueError, AttributeError):
                pass
            text = redact(line)
            output_file.write(text)
            output_file.flush()
            collected.append(text)

        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    control = stop()
                    if control:
                        raise StopRequested(control)
                    if time.monotonic() >= deadline:
                        raise PipelineError("process timeout")
                    for key, _ in selector.select(0.1):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > 8 * 1024 * 1024:
                            raise PipelineError("process output exceeded 8 MiB safety limit")
                        pending += decoder.decode(chunk)
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            emit(line + "\n")
                pending += decoder.decode(b"", final=True)
                if pending:
                    emit(pending)
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return process.returncode, "".join(collected)
        except BaseException:
            # Descendants retain the process group even if the leader already exited.
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            except ProcessLookupError:
                pass
            # A reaped leader does not imply that its group is empty.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise
        finally:
            process.stdout.close()
            write(marker, {"pid": process.pid, "finished": True})


def sanitize(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower() in {"token", "password", "secret", "authorization", "api_key"}
            else sanitize(item)
            for key, item in value.items()
        }
    return value
