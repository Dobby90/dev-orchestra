"""Bounded, streaming subprocess execution and secret-redacted output."""

import codecs
import json
import os
import re
import selectors
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure.process_owner import identity


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
        if k in {"PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT", "SSL_CERT_FILE", "JAVA_HOME"}
    } | {"PYTHONDONTWRITEBYTECODE": "1"}


_SCOPE = ContextVar("execution_scope", default=None)


@contextmanager
def execution_scope(directory):
    scope = _SCOPE.set(directory)
    try:
        yield
    finally:
        _SCOPE.reset(scope)


def execution_directory():
    return _SCOPE.get()


def execute(
    argv,
    cwd: Path,
    timeout,
    log: Path,
    stdin=None,
    stop=lambda: None,
    env=None,
    capture_raw=False,
    on_event=None,
    separate_stderr=False,
):
    log.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as input_file, log.open("w") as output_file:
        input_file.write((stdin or "").encode())
        input_file.seek(0)
        token = uuid.uuid4().hex
        mirror = log.with_suffix(".process.json")
        scope = _SCOPE.get()
        marker = scope / "processes" / (token + ".json") if scope else mirror
        owner_read, owner_write = os.pipe()
        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "dev_orchestra.infrastructure.process_guard",
                    str(owner_read),
                    str(marker),
                    str(mirror),
                    token,
                    json.dumps(identity(os.getpid())),
                    *argv,
                ],
                cwd=cwd,
                env=env or environment(),
                stdin=input_file,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE if capture_raw or separate_stderr else subprocess.STDOUT,
                start_new_session=True,
                pass_fds=(owner_read,),
            )
        except BaseException:
            os.close(owner_write)
            raise
        finally:
            os.close(owner_read)
        deadline = time.monotonic() + timeout
        decoders = {k: codecs.getincrementaldecoder("utf-8")("replace") for k in ["out", "err"]}
        pending, collected, total = {"out": "", "err": ""}, [], 0

        def emit(line, stream):
            try:
                event = json.loads(line)
                if (
                    not capture_raw
                    and isinstance(event, dict)
                    and event.get("item", {}).get("type") == "reasoning"
                ):
                    return
            except (ValueError, AttributeError):
                pass
            else:
                if on_event is not None and stream == "out" and isinstance(event, dict):
                    on_event(sanitize(event))
            text = redact(line)
            output_file.write(text)
            output_file.flush()
            if stream == "out":
                collected.append(line if capture_raw else text)

        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "out")
                if process.stderr:
                    selector.register(process.stderr, selectors.EVENT_READ, "err")
                while selector.get_map():
                    control = stop()
                    if control:
                        raise StopRequested(control)
                    if time.monotonic() >= deadline:
                        raise PipelineError("process timeout")
                    for key, _ in selector.select(0.1):
                        stream = key.data
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > 8 * 1024 * 1024:
                            raise PipelineError("process output exceeded 8 MiB safety limit")
                        pending[stream] += decoders[stream].decode(chunk)
                        while "\n" in pending[stream]:
                            line, pending[stream] = pending[stream].split("\n", 1)
                            emit(line + "\n", stream)
                for stream in pending:
                    pending[stream] += decoders[stream].decode(b"", final=True)
                    if pending[stream]:
                        emit(pending[stream], stream)
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
            record = json.loads(marker.read_text())
            if not record.get("termination_confirmed"):
                raise PipelineError("process termination is not confirmed")
            if record.get("error"):
                raise PipelineError(redact(record.get("error_message", record["error"])))
            return process.returncode, "".join(collected)
        except BaseException:
            # Closing this capability is enough even if the Worker is SIGKILLed.
            os.close(owner_write)
            owner_write = None
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired as error:
                raise PipelineError(
                    "guardian cleanup is not confirmed; intervention required"
                ) from error
            try:
                confirmed = json.loads(marker.read_text()).get("termination_confirmed")
            except (OSError, ValueError):
                confirmed = False
            if not confirmed:
                raise PipelineError("guardian cleanup is not confirmed; intervention required")
            raise
        finally:
            if owner_write is not None:
                os.close(owner_write)
            process.stdout.close()
            if process.stderr:
                process.stderr.close()


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
