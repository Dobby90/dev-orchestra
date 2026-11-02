"""Finite local log replay gate; reports sampled RSS and never calls a model."""

import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path

import psutil

from dev_orchestra.application.service import RunService
from dev_orchestra.config import Config
from dev_orchestra.infrastructure.log_index import LogIndexer
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.infrastructure.process import redact


def main():
    root = Path(tempfile.mkdtemp(prefix="devorchestra-log-gate-")).resolve()
    svc = RunService(
        Config.model_validate(
            {
                "storage": {
                    "database": str(root / "state.db"),
                    "runs_directory": str(root / "runs"),
                    "worktrees_directory": str(root / "worktrees"),
                }
            }
        )
    )
    upgrade(svc.store.path)
    rid = "run-log-gate"
    directory = root / "runs" / rid
    directory.mkdir(parents=True)
    svc.store.create({"id": rid, "directory": str(directory)})
    expected = hashlib.sha256()
    with (directory / "worker.log").open("w") as log:
        for number in range(5500):
            line = f"{number:06d} " + "실시간한글" * 128 + " token=CANARY\n"
            log.write(line)
            expected.update(redact(line).encode())
    process = psutil.Process()
    initial = process.memory_info().rss
    peak = initial
    stopped = threading.Event()

    def sample():
        nonlocal peak
        while not stopped.wait(0.01):
            peak = max(peak, process.memory_info().rss)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.monotonic()
    try:
        index = LogIndexer(svc.store)
        turns = 1
        while index.index(rid, closed=True):
            turns += 1
        indexed_seconds = time.monotonic() - started
        actual = hashlib.sha256()
        cursor = pages = events = max_page = 0
        while True:
            page = svc.events.stream_page(rid, cursor)
            size = sum(len(json.dumps(e, ensure_ascii=False).encode()) for e in page["items"])
            assert size <= 65536
            max_page = max(size, max_page)
            for event in page["items"]:
                assert event["id"] > cursor
                cursor = event["id"]
                assert event["kind"] == "log.appended"
                text = event["data"]["text"]
                assert "CANARY" not in text
                actual.update(text.encode())
                events += 1
            pages += 1
            if not page["has_more"]:
                break
            assert page["next_cursor"] == cursor
        assert actual.hexdigest() == expected.hexdigest()
    finally:
        stopped.set()
        sampler.join()
    peak = max(peak, process.memory_info().rss)
    assert peak - initial < 64 * 1048576
    print(
        json.dumps(
            {
                "status": "PASS",
                "source_bytes": (directory / "worker.log").stat().st_size,
                "index_turns": turns,
                "events": events,
                "pages": pages,
                "largest_page_event_bytes": max_page,
                "index_seconds": round(indexed_seconds, 3),
                "total_seconds": round(time.monotonic() - started, 3),
                "sampled_rss_growth_bytes": peak - initial,
                "unicode_redaction_sha256_match": True,
                "root": str(root),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
