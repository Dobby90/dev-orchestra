"""Request ordering and display timestamps without model execution."""

import pytest

from dev_orchestra.application.queries import RunQueryService
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.migrations import upgrade
from dev_orchestra.infrastructure.store import Store


@pytest.fixture
def queries(tmp_path):
    store = Store(tmp_path / "runs.db")
    upgrade(store.path)
    return RunQueryService(store)


def test_run_pages_sort_by_request_time_with_stable_ties_and_missing_dates(queries):
    for rid, created in [
        ("z-old", "2026-11-01T00:00:00+00:00"),
        ("a-new", "2026-11-05T10:00:00+09:00"),
        ("b-new", "2026-11-05T01:00:00+00:00"),
        ("missing", None),
    ]:
        queries.store.create({"id": rid, "created_at": created})
    cursor, seen = "", []
    while True:
        page = queries.page(limit=1, cursor=cursor)
        seen.extend(item["id"] for item in page["items"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    assert seen == ["b-new", "a-new", "z-old", "missing"]
    queries.store.update("b-new", "planning")
    assert [item["id"] for item in queries.page(state="planning")["items"]] == ["b-new"]
    with pytest.raises(PipelineError, match="invalid run cursor"):
        queries.page(cursor="unknown")


def test_completion_time_uses_approval_event_and_queries_are_read_only(queries):
    queries.store.create({"id": "run", "created_at": "2026-11-05T00:00:00Z"})
    assert queries.summary("run").completed_at is None
    with queries.store.connection() as db:
        db.execute("UPDATE runs SET state='waiting_approval' WHERE id='run'")
    queries.store.approve("run", "a" * 40, "evidence")
    events = queries.store.events("run")
    summary = queries.page()["items"][0]
    assert summary["created_at"] == "2026-11-05T00:00:00Z"
    assert summary["completed_at"] == events[-1]["timestamp"]
    assert queries.store.events("run") == events
    queries.store.update("run", "needs_input")
    assert queries.summary("run").completed_at is None
