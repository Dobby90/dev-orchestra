"""Workspace notifications derived from durable state transitions."""

import json

from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.locking import lock

# Repeated checkpoints in the same state are not new notifications.
TRANSITIONS = """
WITH transitions AS (
 SELECT id,run_id,timestamp,json_extract(data,'$.state') AS state,
 lag(json_extract(data,'$.state')) OVER (PARTITION BY run_id ORDER BY id) AS previous
 FROM events WHERE kind IN ('state','state.changed')
)
SELECT id,run_id,timestamp AS occurred_at,state FROM transitions
WHERE state IN ('needs_input','paused','waiting_approval','waiting_plan_approval','failed')
AND (previous IS NULL OR previous != state)
"""


class NotificationService:
    def __init__(self, store):
        self.store = store
        self.path = store.path.with_suffix(".notifications.json")

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {"through": 0, "ids": []}

    def page(self, before=None, limit=50):
        saved = self._read()
        with self.store.connection() as db:
            db.execute("BEGIN")
            cursor = db.execute(
                "SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name='events'),0)"
            ).fetchone()[0]
            rows = db.execute(
                TRANSITIONS + " AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
                (before, before, limit + 1),
            ).fetchall()
            unread = db.execute(
                "SELECT count(*) FROM (" + TRANSITIONS + ") WHERE id>? "
                "AND id NOT IN (SELECT value FROM json_each(?))",
                (saved["through"], json.dumps(saved["ids"])),
            ).fetchone()[0]
        items = [
            dict(row) | {"read": row["id"] <= saved["through"] or row["id"] in saved["ids"]}
            for row in rows[:limit]
        ]
        return {
            "items": items,
            "unread_count": unread,
            "cursor": cursor,
            "next_cursor": items[-1]["id"] if len(rows) > limit else None,
        }

    def mark_read(self, ids, through=None):
        with lock(self.path.with_suffix(".lock"), wait=True):
            saved = self._read()
            with self.store.connection() as db:
                high = db.execute(
                    "SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name='events'),0)"
                ).fetchone()[0]
            cursor = max(saved["through"], min(through or 0, high))
            write(
                self.path,
                {
                    "through": cursor,
                    "ids": sorted({i for i in saved["ids"] + ids if cursor < i <= high}),
                },
            )
