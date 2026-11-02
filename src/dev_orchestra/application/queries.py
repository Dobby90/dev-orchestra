"""Read-only database queries; recovery is an explicit separate service."""

from dev_orchestra.application.contracts import RunSummary


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
