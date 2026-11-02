"""Transport-independent events application service."""

from pathlib import Path

from dev_orchestra.domain.models import PipelineError


class EventService:
    def __init__(self, store):
        self.store = store

    def history(self, run_id, after=0, limit=100):
        self.store.get(run_id)
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise PipelineError("invalid event cursor or limit")
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
                (run_id, after, limit + 1),
            ).fetchall()
        items = [dict(row) for row in rows[:limit]]
        return {
            "items": items,
            "has_more": len(rows) > limit,
            "next_cursor": items[-1]["id"] if items else after,
        }

    def log_paths(self, run_id, role=None):
        if role is not None and role not in {"planner", "developer", "tester", "reviewer", "fixer"}:
            raise PipelineError("invalid role filter")
        directory = Path(self.store.get(run_id)["directory"])
        paths = sorted(directory.glob("iterations/*/*/*/*.log"))
        if role is None and (directory / "workflow.log").exists():
            paths.insert(0, directory / "workflow.log")
        return [p for p in paths if role is None or p.parent.parent.name == role]

    def log_chunks(self, run_id, cursor=None, role=None, limit=65536):
        """Replay durable file logs using per-attempt paths and byte offsets."""
        if not 1 <= limit <= 1048576:
            raise PipelineError("invalid log chunk limit")
        run = self.store.get(run_id)
        root = Path(run["directory"])
        cursor = dict(cursor or {})
        chunks = []
        for path in self.log_paths(run_id, role):
            key = str(path.relative_to(root))
            offset = cursor.get(key, 0)
            if not isinstance(offset, int) or offset < 0 or offset > path.stat().st_size:
                raise PipelineError("invalid log cursor")
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read(limit)
                # Finish a UTF-8 code point so byte cursors never lose Korean text.
                if data:
                    import codecs

                    decoder = codecs.getincrementaldecoder("utf-8")("replace")
                    decoder.decode(data, final=False)
                    while decoder.getstate()[0]:
                        extra = handle.read(1)
                        if not extra:
                            break
                        data += extra
                        decoder.decode(extra, final=False)
            if data:
                chunks.append(
                    {
                        "run_id": run_id,
                        "log_id": key,
                        "offset": offset,
                        "text": data.decode("utf-8", errors="replace"),
                    }
                )
                cursor[key] = offset + len(data)
        return {"schema_version": 1, "chunks": chunks, "cursor": cursor}
