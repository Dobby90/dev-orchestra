"""Crash-replayable redacted log views. Source files never become HTTP paths."""

import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained, write
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process import redact
from dev_orchestra.infrastructure.store import now


def digest(data):
    return hashlib.sha256(data).hexdigest()


def utf8_chunks(text, size=4096):
    data = text.encode("utf-8")
    while data:
        end = min(size, len(data))
        while end < len(data) and data[end] & 0xC0 == 0x80:
            end -= 1
        yield data[:end]
        data = data[end:]


@dataclass
class IndexBudget:
    files: int
    deadline: float

    def take(self):
        if self.files <= 0 or time.monotonic() >= self.deadline:
            return False
        self.files -= 1
        return True


class LogIndexer:
    def __init__(self, store):
        self.store = store

    def index(self, run_id, closed=False, *, budget=None, rescan_interval=0):
        run = self.store.get(run_id)
        root = Path(run["directory"])
        pending = False
        with lock(root / "log-index.lock"):
            # Private scheduling metadata is atomic and does not change Run versions.
            # It is only a scan hint; committed DB offsets remain authoritative.
            state_path = root / "log-index-state.json"
            try:
                state = json.loads(state_path.read_text())
                if (
                    not isinstance(state, dict)
                    or not isinstance(state.get("cursor", ""), str)
                    or not isinstance(state.get("completed_at", 0), (int, float, type(None)))
                ):
                    state = {}
            except (OSError, ValueError):
                state = {}
            same = state.get("closed") == closed and state.get("version") == run["version"]
            if (
                same
                and closed
                and state.get("complete")
                and time.time() < (state.get("completed_at") or 0) + rescan_interval
            ):
                return False
            cursor = state.get("cursor", "") if same and budget is not None else ""
            last = cursor
            paths = [
                *root.glob("*.log"),
                *root.glob("iterations/*/*/*/*.log"),
                *root.glob("git/*.log"),
                *root.glob("preflight/*.log"),
            ]
            for path in sorted(paths, key=lambda p: str(p.relative_to(root))):
                relative = str(path.relative_to(root))
                if relative <= cursor:
                    continue
                if budget is not None and not budget.take():
                    pending = True
                    break
                # No symlink or directory traversal, including legacy log entries.
                try:
                    source = contained(root, relative)
                    if not source.is_file():
                        last = relative
                        continue
                except PipelineError:
                    last = relative
                    continue
                if self._source(
                    run, source, relative, closed, deadline=budget.deadline if budget else None
                ):
                    pending = True
                    if budget is not None:
                        break
                    continue
                last = relative
            write(
                state_path,
                {
                    "version": run["version"],
                    "closed": closed,
                    "cursor": last if pending and budget is not None else "",
                    "complete": not pending,
                    "completed_at": time.time() if not pending else None,
                },
            )
        return pending

    def _source(self, run, path, relative, closed, *, deadline=None):
        stat = path.stat()
        identity = json.dumps([stat.st_dev, stat.st_ino])
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM log_sources WHERE run_id=? AND relative_path=? ORDER BY generation DESC LIMIT 1",
                (run["id"], relative),
            ).fetchone()
        role = None
        step = None
        parts = Path(relative).parts
        if parts[0] == "iterations" and len(parts) >= 5:
            role = parts[2]
            try:
                iteration = int(parts[1])
                attempt = int(parts[3].split("-")[1])
                with self.store.connection() as db:
                    found = db.execute(
                        "SELECT id FROM steps WHERE run_id=? AND role=? AND iteration=? AND attempt=?",
                        (run["id"], role, iteration, attempt),
                    ).fetchone()
                    step = found[0] if found else None
            except (ValueError, IndexError):
                pass
        if row is None or row["identity"] != identity or stat.st_size < row["indexed_offset"]:
            generation = 1 if row is None else row["generation"] + 1
            log_id = "log-" + uuid.uuid4().hex
            with self.store.connection() as db:
                db.execute(
                    "INSERT INTO log_sources(id,run_id,relative_path,generation,identity,role,step_id) VALUES(?,?,?,?,?,?,?)",
                    (log_id, run["id"], relative, generation, identity, role, step),
                )
                row = db.execute("SELECT * FROM log_sources WHERE id=?", (log_id,)).fetchone()
        view = Path(run["directory"]) / "log-views" / (row["id"] + ".log")
        if stat.st_size == row["indexed_offset"] and view.exists():
            size = view.stat().st_size
            if size < row["view_offset"]:
                raise PipelineError("indexed log view missing bytes")
            if size == row["view_offset"]:
                return False
        view.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # File append precedes DB commit. Remove only an uncommitted private tail.
        fd = os.open(view, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.fstat(fd).st_size < row["view_offset"]:
                raise PipelineError("indexed log view missing bytes")
            if os.fstat(fd).st_size != row["view_offset"]:
                os.ftruncate(fd, row["view_offset"])
            os.lseek(fd, row["view_offset"], os.SEEK_SET)
            raw_offset = row["indexed_offset"]
            view_offset = row["view_offset"]
            with path.open("rb") as source:
                source.seek(raw_offset)
                # Bound each indexing turn. A partial line stays pending until complete.
                budget = 262144
                timed_out = False
                while budget > 0:
                    if deadline is not None and time.monotonic() >= deadline:
                        timed_out = True
                        break
                    line = source.readline(8 * 1024 * 1024 + 1)
                    if not line:
                        break
                    if len(line) > 8 * 1024 * 1024:
                        raise PipelineError("log line exceeds safety limit")
                    if not line.endswith(b"\n") and not closed:
                        break
                    budget -= len(line)
                    text = redact(line.decode("utf-8", "replace"))
                    level = "unknown"
                    if relative == "workflow.log":
                        match = re.match(
                            r"^\d{4}-\d\d-\d\d .+? (DEBUG|INFO|WARNING|ERROR|CRITICAL) ", text
                        )
                        if match:
                            level = match[1].lower()
                    spans = []
                    for chunk in utf8_chunks(text):
                        start = view_offset
                        remaining = memoryview(chunk)
                        while remaining:
                            written = os.write(fd, remaining)
                            if written <= 0:
                                raise OSError("log append made no progress")
                            remaining = remaining[written:]
                        view_offset += len(chunk)
                        spans.append((start, view_offset, digest(chunk)))
                    os.fsync(fd)
                    raw_offset += len(line)
                    with self.store.transaction() as db:
                        for start, end, sha in spans:
                            cursor = db.execute(
                                "INSERT INTO events(run_id,timestamp,kind,data) VALUES(?,?,?,?)",
                                (
                                    run["id"],
                                    now(),
                                    "log.appended",
                                    json.dumps({"schema_version": 1, "log_id": row["id"]}),
                                ),
                            )
                            db.execute(
                                "INSERT INTO log_spans VALUES(?,?,?,?,?,?)",
                                (cursor.lastrowid, row["id"], start, end, sha, level),
                            )
                        db.execute(
                            "UPDATE log_sources SET indexed_offset=?,view_offset=? WHERE id=?",
                            (raw_offset, view_offset, row["id"]),
                        )
        finally:
            os.close(fd)
        return budget <= 0 or timed_out

    def replay(self, run_id, event_id):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT s.*,p.offset,p.end_offset,p.sha256,p.level FROM log_sources s JOIN log_spans p ON p.log_id=s.id WHERE p.event_id=? AND s.run_id=?",
                (event_id, run_id),
            ).fetchone()
        if not row:
            raise PipelineError("log event unavailable")
        root = Path(self.store.get(run_id)["directory"])
        path = contained(root, "log-views/" + row["id"] + ".log")
        try:
            with path.open("rb") as handle:
                handle.seek(row["offset"])
                data = handle.read(row["end_offset"] - row["offset"])
        except OSError as error:
            raise PipelineError("log view unavailable") from error
        if digest(data) != row["sha256"]:
            raise PipelineError("log view integrity error")
        return {
            "log_id": row["id"],
            "generation": row["generation"],
            "offset": row["offset"],
            "end_offset": row["end_offset"],
            "text": data.decode("utf-8"),
            "level": row["level"],
            "role": row["role"],
            "step_id": row["step_id"],
            "source": row["relative_path"],
        }
