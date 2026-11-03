"""Bounded model discovery via the official app-server protocol; never starts a turn."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import TypeAdapter

from dev_orchestra.config import ModelName, ReasoningEffort
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.process import execute


def list_models(command):
    with tempfile.TemporaryDirectory(prefix="devorchestra-models-") as temp:
        rc, output = execute(
            [sys.executable, "-m", __name__, command],
            Path(temp),
            20,
            Path(temp) / "catalog.log",
            capture_raw=True,
        )
        try:
            if rc:
                raise ValueError()
            rows = json.loads(output)
            if not isinstance(rows, list) or not 1 <= len(rows) <= 1000:
                raise ValueError()
            result = []
            for row in rows:
                result.append(
                    {
                        "model": TypeAdapter(ModelName).validate_python(row["model"]),
                        "reasoning_efforts": TypeAdapter(list[ReasoningEffort]).validate_python(
                            row["reasoning_efforts"]
                        ),
                    }
                )
            return result
        except (ValueError, TypeError, KeyError) as error:
            raise PipelineError("Codex model catalog unavailable") from error


def discover(command):
    # This helper runs under execute's ownership guardian. It also owns any remaining
    # descendants if app-server does not exit promptly after stdin closes.
    process = subprocess.Popen(
        [command, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    sequence = 0

    def send(value):
        process.stdin.write(json.dumps(value) + "\n")
        process.stdin.flush()

    def rpc(method, params):
        nonlocal sequence
        sequence += 1
        send({"id": sequence, "method": method, "params": params})
        for _ in range(256):
            line = process.stdout.readline(1048577)
            if not line or len(line) > 1048576:
                raise ValueError("invalid catalog response")
            message = json.loads(line)
            if message.get("id") == sequence:
                if "error" in message:
                    raise ValueError("catalog request rejected")
                return message["result"]
        raise ValueError("catalog event limit exceeded")

    try:
        rpc("initialize", {"clientInfo": {"name": "devorchestra", "version": "0.1.0"}})
        send({"method": "initialized"})
        models, cursor = [], None
        for _ in range(10):
            page = rpc("model/list", {"limit": 100, "cursor": cursor, "includeHidden": False})
            for row in page["data"]:
                models.append(
                    {
                        "model": row["model"],
                        "reasoning_efforts": [
                            item["reasoningEffort"] for item in row["supportedReasoningEfforts"]
                        ],
                    }
                )
            cursor = page.get("nextCursor")
            if cursor is None:
                print(json.dumps(models), flush=True)
                return
        raise ValueError("catalog page limit exceeded")
    finally:
        process.stdin.close()
        process.stdout.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


if __name__ == "__main__":
    try:
        discover(sys.argv[1])
    except Exception:
        # Authentication, paths and provider error payloads stay inside the local process.
        sys.exit(1)
