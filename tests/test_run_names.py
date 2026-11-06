"""Run display metadata remains independent of execution evidence and schema upgrades."""

import json

import pytest
from pydantic import ValidationError
from test_workflow import service as service

from dev_orchestra.application.web_contracts import Proposal


@pytest.mark.parametrize("value", [None, "", "   ", "x" * 101])
def test_proposal_requires_nonblank_bounded_name(value):
    body = {"project_id": "prj-" + "a" * 32, "spec": {"kind": "text", "text": "PRD"}}
    if value is not None:
        body["name"] = value
    with pytest.raises(ValidationError):
        Proposal.model_validate(body)


def test_legacy_name_uses_id_without_mutating_history(service):
    svc, rid, _ = service
    with svc.store.connection() as db:
        data = json.loads(db.execute("SELECT data FROM runs WHERE id=?", (rid,)).fetchone()[0])
        data.pop("name", None)
        raw = json.dumps(data)
        db.execute("UPDATE runs SET data=? WHERE id=?", (raw, rid))
    before = svc.store.events(rid)
    assert svc.queries.summary(rid).name == rid
    assert svc.queries.snapshot(rid)["name"] == rid
    assert svc.queries.page()["items"][0]["name"] == rid
    with svc.store.connection() as db:
        assert db.execute("SELECT data FROM runs WHERE id=?", (rid,)).fetchone()[0] == raw
        assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 6
    assert svc.store.events(rid) == before


def test_summary_reports_saved_push_and_worktree_state(service):
    svc, rid, _ = service
    sha = "a" * 40
    cases = [
        (None, "not_pushed"),
        ({"status": "running"}, "running"),
        ({"status": "success", "sha": sha}, "success"),
        ({"status": "success", "sha": "b" * 40}, "stale"),
        ({"status": "access_denied"}, "failed"),
    ]
    for result, expected in cases:
        svc.store.update(
            rid, name="로그인 개선", final_sha=sha, push_result=result, worktree_removed=True
        )
        summary = svc.queries.summary(rid)
        assert summary.name == "로그인 개선"
        assert summary.push_status == expected
        assert summary.worktree_removed
