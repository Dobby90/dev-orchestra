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


def test_project_names_follow_database_relation_and_include_archived_projects(queries):
    with queries.store.connection() as db:
        db.execute(
            "INSERT INTO projects(id,name,repository_path,git_common_directory,identity,"
            "default_base_branch,policy,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "project",
                "예제 프로젝트",
                "/example",
                "/example/.git",
                "identity",
                "main",
                "{}",
                "now",
                "now",
            ),
        )
    queries.store.create({"id": "linked", "project_name": "신뢰하지 않는 이름"})
    queries.store.create({"id": "cli", "project_name": "신뢰하지 않는 이름"})
    with queries.store.connection() as db:
        db.execute("UPDATE runs SET project_id='project' WHERE id='linked'")
    assert queries.summary("linked").project_name == "예제 프로젝트"
    assert queries.summary("cli").project_name is None
    with queries.store.connection() as db:
        db.execute("UPDATE projects SET name='변경된 이름', archived_at='now' WHERE id='project'")
    items = {item["id"]: item for item in queries.page()["items"]}
    assert items["linked"]["project_name"] == "변경된 이름"
    assert items["cli"]["project_name"] is None


def test_schema_two_history_has_no_project_name(tmp_path):
    store = Store(tmp_path / "legacy.db")
    store.create({"id": "legacy", "project_name": "위조된 이름"})
    assert RunQueryService(store).summary("legacy").project_name is None
