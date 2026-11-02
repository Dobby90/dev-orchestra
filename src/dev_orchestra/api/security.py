"""Loopback sessions, explicit origin/host checks, and CSRF protection."""

import hashlib
import hmac
import os
import secrets
import time
from collections import deque
from pathlib import Path
from threading import Lock

from dev_orchestra.application.errors import ApplicationError

COOKIE = "devorchestra_session"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def runtime_key(path: Path):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink():
            raise ApplicationError("UNSAFE_PATH", "runtime key must not be a symlink")
        value = path.read_bytes()
        if len(value) != 32:
            raise ApplicationError("INVALID_KEY", "invalid runtime key")
        return value
    with os.fdopen(fd, "wb") as f:
        value = secrets.token_bytes(32)
        f.write(value)
        f.flush()
        os.fsync(f.fileno())
    return value


def bootstrap(store):
    code = secrets.token_urlsafe(32)
    with store.connection() as db:
        db.execute("INSERT INTO web_bootstrap_codes VALUES(?,?)", (digest(code), time.time() + 60))
    return code


class Security:
    def __init__(self, store, key):
        self.store, self.key = store, key
        self.attempts, self.gate = deque(), Lock()

    def csrf(self, token):
        return hmac.new(self.key, token.encode(), hashlib.sha256).hexdigest()

    def exchange(self, code):
        with self.gate:
            stamp = time.monotonic()
            while self.attempts and self.attempts[0] < stamp - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 5:
                raise ApplicationError("LOCAL_RATE_LIMIT", "too many session attempts")
            self.attempts.append(stamp)
        token = secrets.token_urlsafe(32)
        expires = time.time() + 28800
        with self.store.transaction() as db:
            row = db.execute(
                "DELETE FROM web_bootstrap_codes WHERE code_hash=? AND expires_at>? RETURNING code_hash",
                (digest(code), time.time()),
            ).fetchone()
            if not row:
                raise ApplicationError("SESSION_REQUIRED", "bootstrap invalid or expired")
            db.execute("INSERT INTO web_sessions VALUES(?,?)", (digest(token), expires))
        return token, expires

    def authenticate(self, token):
        if not token:
            raise ApplicationError("SESSION_REQUIRED", "session required")
        with self.store.connection() as db:
            row = db.execute(
                "SELECT expires_at FROM web_sessions WHERE session_hash=? AND expires_at>?",
                (digest(token), time.time()),
            ).fetchone()
        if not row:
            raise ApplicationError("SESSION_REQUIRED", "session expired")
        return row["expires_at"]
