"""Read-only database queries; recovery is an explicit separate service."""

import json

from dev_orchestra.application.contracts import RunSummary
from dev_orchestra.domain.models import PipelineError

_COMPLETION_EVENT = (
    "(kind='approved' OR (kind='state' AND json_extract(data,'$.state')='completed'))"
)


def delivery_status(run):
    result = run.get("push_result") or {}
    status = result.get("status")
    if not status:
        push_status = "not_pushed"
    elif status == "running":
        push_status = "running"
    elif status == "success":
        push_status = (
            "success" if run.get("final_sha") and result.get("sha") == run["final_sha"] else "stale"
        )
    else:
        push_status = "failed"
    return {
        "worktree_removed": bool(run.get("worktree_removed")),
        "push_status": push_status,
    }


class RunQueryService:
    def __init__(self, store):
        self.store = store

    def get(self, run_id):
        # Internal compatibility view. HTTP adapters must use explicit public DTOs.
        return self.store.get(run_id)

    def list(self):
        return self.store.list()

    def summary(self, run_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise PipelineError(f"unknown run: {run_id}")
            return self._summary(
                row, self._project_name(db, row), self._completed_at(db, run_id, row["state"])
            )

    @staticmethod
    def _summary(row, project_name, completed_at):
        run = json.loads(row["data"])
        values = {k: run[k] for k in RunSummary.model_fields if k in run}
        values.update(delivery_status(run))
        values.update(
            id=row["id"],
            state=row["state"],
            version=row["version"],
            control=row["control"],
            current_role=run.get("next_role") if run.get("next_role") != "done" else None,
            project_name=project_name,
            completed_at=completed_at,
        )
        return RunSummary.model_validate(values)

    @staticmethod
    def _project_name(db, row):
        # Schema 2 history has no project relation. Use the DB relation rather
        # than untrusted run data, and retain names for archived projects.
        if "project_id" not in row.keys() or not row["project_id"]:
            return None
        project = db.execute(
            "SELECT name FROM projects WHERE id=?", (row["project_id"],)
        ).fetchone()
        return project["name"] if project else None

    @staticmethod
    def _completed_at(db, run_id, state):
        if state != "completed":
            return None
        row = db.execute(
            f"SELECT timestamp FROM events WHERE run_id=? AND {_COMPLETION_EVENT} "
            "ORDER BY id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        return row["timestamp"] if row else None

    def steps(self, run_id):
        run = self.get(run_id)
        from dev_orchestra.application.progress import role_progress

        return [step | role_progress(self.store, run, step) for step in self.store.steps(run_id)]

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
                | delivery_status(data)
            ).model_dump(mode="json")
            value["project_name"] = self._project_name(db, row)
            value["event_cursor"] = db.execute(
                "SELECT coalesce((SELECT seq FROM sqlite_sequence WHERE name='events'),0)"
            ).fetchone()[0]
            value["project_id"] = row["project_id"] if "project_id" in row.keys() else None
            from dev_orchestra.infrastructure.process import sanitize

            approval = data.get("plan_approval") or {}
            value.update(
                test_scope=data.get("test_scope", "full"),
                auto_approve_plan=data.get("auto_approve_plan", False),
                automatic_plan_approved_at=(
                    approval.get("approved_at") if approval.get("source") == "automatic" else None
                ),
                base_branch=data.get("base"),
                created_at=data.get("created_at"),
                completed_at=self._completed_at(db, run_id, row["state"]),
                max_fix_attempts=data.get("config", {}).get("workflow", {}).get("max_fix_attempts"),
                questions=sanitize(data.get("questions", [])),
                error=sanitize(data.get("error")),
                test_change_pending=bool(
                    data.get("test_change_proposal") or data.get("test_change_application")
                ),
                test_change_count=len(data.get("test_change_records", [])),
            )
        from dev_orchestra.application.observations import observe

        data["project_id"] = value["project_id"]
        from dev_orchestra.application.plan_approval import binding

        value.update(plan=data.get("plan"), criteria=data.get("criteria", []), plan_hash=None)
        if data["state"] == "waiting_plan_approval":
            value["plan_hash"] = binding(self.store, data)["plan_hash"]
        return value | observe(self.store, data)

    def page(self, limit=50, cursor="", state=None, project_id=None, *, states=None):
        if not 1 <= limit <= 100:
            raise PipelineError("invalid page size")
        filters, parameters = [], []
        if state is not None:
            filters.append("state=?")
            parameters.append(state)
        if project_id is not None:
            filters.append("project_id=?")
            parameters.append(project_id)
        if states:
            filters.append(f"state IN ({','.join('?' for _ in states)})")
            parameters.extend(states)
        with self.store.connection() as db:
            # Keep the ID cursor, but compare its request time and ID together so
            # equal timestamps and multiple pages retain a deterministic order.
            requested = "coalesce(julianday(json_extract(data,'$.created_at')),0)"
            if cursor:
                if not db.in_transaction:
                    db.execute("BEGIN")
                anchor = db.execute(
                    f"SELECT {requested} AS requested FROM runs WHERE id=?", (cursor,)
                ).fetchone()
                if anchor is None:
                    raise PipelineError("invalid run cursor")
                filters.append(f"({requested},id)<(?,?)")
                parameters.extend([anchor["requested"], cursor])
            where = " WHERE " + " AND ".join(filters) if filters else ""
            # Select the page before looking up related names and completion events.
            rows = db.execute(
                f"WITH page AS (SELECT *, {requested} AS requested FROM runs{where} "
                "ORDER BY requested DESC,id DESC LIMIT ?) "
                "SELECT page.*, projects.name AS project_name, "
                "CASE WHEN page.state='completed' THEN ("
                f"SELECT timestamp FROM events WHERE run_id=page.id AND {_COMPLETION_EVENT} "
                "ORDER BY id DESC LIMIT 1) END AS completed_at "
                "FROM page LEFT JOIN projects ON projects.id=page.project_id "
                "ORDER BY page.requested DESC,page.id DESC",
                (*parameters, limit + 1),
            ).fetchall()
        items = [
            self._summary(row, row["project_name"], row["completed_at"]).model_dump(mode="json")
            for row in rows[:limit]
        ]
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
