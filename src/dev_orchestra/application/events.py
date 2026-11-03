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

    def stream_page(self, run_id, after=0, limit=200, budget=65536):
        import json

        from dev_orchestra.infrastructure.log_index import LogIndexer
        from dev_orchestra.infrastructure.process import sanitize

        page = self.history(run_id, after, limit)
        items = []
        size = 0
        aliases = {
            "state": "state.changed",
            "control_requested": "control.requested",
            "step_started": "step.started",
            "step_completed": "step.completed",
            "approved": "approval.recorded",
        }
        for row in page["items"]:
            kind = aliases.get(row["kind"], row["kind"])
            data = json.loads(row["data"])
            if kind == "log.appended":
                try:
                    data = LogIndexer(self.store).replay(run_id, row["id"])
                except PipelineError:
                    kind = "log.unavailable"
                    data = {"reason": "log view missing or modified"}
            else:
                data = sanitize(
                    {k: v for k, v in data.items() if k not in {"artifact", "claim_token", "token"}}
                )
            event = {
                "schema_version": 1,
                "id": row["id"],
                "run_id": run_id,
                "kind": kind,
                "occurred_at": row["timestamp"],
                "data": data,
            }
            length = len(json.dumps(event, ensure_ascii=False).encode())
            if length > budget:
                event = {
                    **event,
                    "kind": "event.unavailable",
                    "data": {"reason": "event exceeds page budget"},
                }
                length = len(json.dumps(event).encode())
            if size + length > budget:
                break
            items.append(event)
            size += length
        return {
            "items": items,
            "next_cursor": items[-1]["id"] if items else after,
            "has_more": page["has_more"] or len(items) < len(page["items"]),
        }

    def log_window(self, run_id, before=None, limit=200, step_id=None):
        """Read a bounded newest-first window, then return display order and live cursor."""
        import json

        self.store.get(run_id)
        if before is not None and (type(before) is not int or before < 0):
            raise PipelineError("invalid log cursor")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise PipelineError("invalid log limit")
        if step_id is not None and (type(step_id) is not int or step_id < 1):
            raise PipelineError("invalid step filter")
        with self.store.connection() as db:
            latest = db.execute(
                "SELECT coalesce(max(id),0) FROM events WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            boundary = latest + 1 if before is None else min(before, latest + 1)
            rows = db.execute(
                "SELECT e.id FROM events e JOIN log_spans p ON p.event_id=e.id "
                "JOIN log_sources s ON s.id=p.log_id "
                "WHERE e.run_id=? AND e.id<? AND (? IS NULL OR s.step_id=?) "
                "ORDER BY e.id DESC LIMIT ?",
                (run_id, boundary, step_id, step_id, limit + 1),
            ).fetchall()
        items = []
        size = 0
        for row in rows[:limit]:
            event = self.stream_page(run_id, row[0] - 1, 1)["items"][0]
            length = len(json.dumps(event, ensure_ascii=False).encode())
            if size + length > 65536:
                break
            items.append(event)
            size += length
        return {
            "items": list(reversed(items)),
            "latest_cursor": latest,
            "before_cursor": items[-1]["id"] if items else boundary,
            "has_more": len(rows) > len(items),
        }

    def high_water(self):
        with self.store.connection() as db:
            return db.execute(
                "SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name='events'),0)"
            ).fetchone()[0]
