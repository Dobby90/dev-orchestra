"""Owned API child using a pre-bound loopback socket. No Worker is owned by ASGI."""

import json
import socket
import sys
from pathlib import Path

import uvicorn

from dev_orchestra.api.main import create_app
from dev_orchestra.config import Config
from dev_orchestra.execution.lifetime import watch_parent

value = json.loads(Path(sys.argv[1]).read_text())
sock = socket.socket(fileno=int(sys.argv[2]))
app = create_app(
    Config.model_validate(value["config"]),
    [Path(p) for p in value["roots"]],
    value["port"],
    static_directory=value.get("assets"),
)
server = uvicorn.Server(
    uvicorn.Config(
        app,
        host="127.0.0.1",
        port=value["port"],
        proxy_headers=False,
        log_level="warning",
        timeout_graceful_shutdown=3,
    )
)


def parent_stopped():
    server.should_exit = True


watch_parent(int(sys.argv[3]), parent_stopped)
server.run(sockets=[sock])
