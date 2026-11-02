"""Read-only database queries; recovery is an explicit separate service."""

import json

from dev_orchestra.application.contracts import RunSummary
from dev_orchestra.domain.models import PipelineError


class RunQueryService:
    def __init__(self, store):
        self.store = store

    def get(self, run_id):
        # Internal compatibility view. HTTP adapters must use explicit public DTOs.
        return self.store.get(run_id)

    def list(self):
        return self.store.list()

    def summary(self, run_id):
        run = self.get(run_id)
        values = {k: run[k] for k in RunSummary.model_fields if k in run}
        values["current_role"] = run.get("next_role") if run.get("next_role") != "done" else None
        return RunSummary.model_validate(values)

    def steps(self, run_id):
        self.get(run_id)
        return self.store.steps(run_id)

    def snapshot(self, run_id):
        with self.store.connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise PipelineError(f"unknown run: {run_id}")
            data = json.loads(row["data"])
            data.update(state=row["state"], version=row["version"], control=row["control"])
            data["current_role"] = (
                data.get("next_role") if data.get("next_role") != "done" else None
            )
            value = RunSummary.model_validate(
                {k: v for k, v in data.items() if k in RunSummary.model_fields}
            ).model_dump(mode="json")
            value["event_cursor"] = db.execute("SELECT coalesce(max(id),0) FROM events").fetchone()[
                0
            ]
            value["project_id"] = row["project_id"] if "project_id" in row.keys() else None
            from dev_orchestra.infrastructure.process import sanitize

            value.update(
                base_branch=data.get("base"),
                created_at=data.get("created_at"),
                max_fix_attempts=data.get("config", {}).get("workflow", {}).get("max_fix_attempts"),
                questions=sanitize(data.get("questions", [])),
                error=sanitize(data.get("error")),
            )
        return value

    def page(self, limit=50, cursor="", state=None, project_id=None):
        if not 1 <= limit <= 100:
            raise PipelineError("invalid page size")
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT id FROM runs WHERE id>? AND (? IS NULL OR state=?) AND (? IS NULL OR project_id=?) ORDER BY id LIMIT ?",
                (cursor, state, state, project_id, project_id, limit + 1),
            ).fetchall()
        items = [self.summary(r["id"]).model_dump(mode="json") for r in rows[:limit]]
        return {
            "items": items,
            "has_more": len(rows) > limit,
            "next_cursor": items[-1]["id"] if len(rows) > limit else None,
        }

    def dashboard(self):
        with self.store.connection() as db:
            counts = {
                r[0]: r[1] for r in db.execute("SELECT state,count(*) FROM runs GROUP BY state")
            }
            projects = db.execute(
                "SELECT count(*) FROM projects WHERE archived_at IS NULL"
            ).fetchone()[0]
        return {"counts_by_state": counts, "project_count": projects}
