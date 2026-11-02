"""Shared services exercised without HTTP, Node or model calls."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service
from typer.testing import CliRunner

from dev_orchestra.application.contracts import ApprovalCommand, CreateRun
from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.projects import ProjectService
from dev_orchestra.cli import app
from dev_orchestra.infrastructure.files import write
from dev_orchestra.orchestration.runner import Runner


def test_queries_do_not_reconcile_or_generate_reports(service):
    svc, rid, _ = service
    svc.store.update(rid, "planning")
    directory = Path(svc.store.get(rid)["directory"])
    write(directory / "worker.json", {"pid": -1, "create_time": 1})
    before = svc.store.events(rid)
    assert svc.queries.summary(rid).state == "planning"
    assert svc.queries.get(rid)["state"] == "planning"
    assert svc.queries.list()[0]["id"] == rid
    assert svc.store.events(rid) == before
    assert not (directory / "final-report.md").exists()
    assert svc.status(rid)["state"] == "failed"


def test_public_summary_excludes_private_snapshots(service):
    svc, rid, _ = service
    result = svc.queries.summary(rid).model_dump(mode="json")
    assert result["id"] == rid and result["state"] == "queued"
    assert not {"spec", "config", "directory", "repo", "answers"} & result.keys()
    json.dumps(result)


def test_typed_creation_preserves_original_checkout(service):
    svc, rid, repo = service
    before = (repo / "main.py").read_bytes()
    spec = Path(svc.store.get(rid)["directory"]) / "spec.md"
    new = svc.create_run(CreateRun(repo=repo, spec=spec, max_fixes=0))
    assert svc.queries.get(new)["config"]["workflow"]["max_fix_attempts"] == 0
    assert (repo / "main.py").read_bytes() == before
    assert svc.config.workflow.max_fix_attempts == 3


@pytest.mark.parametrize("value", [-1, True, "3"])
def test_creation_contract_rejects_invalid_budget(value):
    with pytest.raises(ValidationError):
        CreateRun(repo=Path("/tmp/repo"), spec=Path("/tmp/spec"), max_fixes=value)


def test_approval_preconditions_and_shared_facade(service):
    svc, rid, _ = service
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    run = svc.queries.get(rid)
    with pytest.raises(ApplicationError) as error:
        svc.approvals.approve_command(
            ApprovalCommand(
                run_id=rid, expected_version=run["version"] - 1, expected_sha=run["final_sha"]
            )
        )
    assert error.value.code == "VERSION_CONFLICT"
    with pytest.raises(ApplicationError) as error:
        svc.approvals.approve_command(
            ApprovalCommand(run_id=rid, expected_version=run["version"], expected_sha="0" * 40)
        )
    assert error.value.code == "CANDIDATE_CHANGED"
    assert svc.queries.get(rid)["state"] == "waiting_approval"
    svc.approvals.approve_command(
        ApprovalCommand(run_id=rid, expected_version=run["version"], expected_sha=run["final_sha"])
    )
    assert svc.status(rid)["state"] == "completed"


def test_event_history_pages_without_state_changes(service):
    svc, rid, _ = service
    svc.store.update(rid, "planning")
    svc.store.control(rid, "pause")
    first = svc.events.history(rid, limit=1)
    assert first["has_more"]
    second = svc.events.history(rid, after=first["next_cursor"], limit=1)
    assert second["items"][0]["id"] > first["items"][0]["id"]
    assert not second["has_more"]
    assert svc.queries.get(rid)["state"] == "planning"
    with pytest.raises(RuntimeError):
        svc.events.history(rid, after=-1)


def test_project_validation_and_policy_snapshot(service, tmp_path):
    svc, _, repo = service
    projects = ProjectService([tmp_path.resolve()])
    before = (repo / "main.py").read_bytes()
    result = projects.validate_repository(repo.resolve(), "main")
    assert result.repository_path == repo.resolve()
    assert len(result.base_sha) == 40
    assert (repo / "main.py").read_bytes() == before
    snapshot = projects.validate_policy(svc.config)
    snapshot.workflow.max_fix_attempts = 10
    assert svc.config.workflow.max_fix_attempts == 3
    with pytest.raises(ApplicationError, match="outside allowed roots"):
        ProjectService([]).validate_repository(repo.resolve(), "main")
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    with pytest.raises(ApplicationError, match="symlink"):
        projects.validate_repository(link, "main")
    sub = repo / "nested"
    sub.mkdir()
    with pytest.raises(ApplicationError, match="repository root"):
        projects.validate_repository(sub, "main")
    bad = svc.config.model_copy(deep=True)
    bad.review.blocking_severities = ["low"]
    with pytest.raises(ApplicationError, match="critical and high"):
        projects.validate_policy(bad)


def test_cli_list_compatibility_and_complete_logs(service):
    svc, rid, _ = service
    runner = CliRunner()
    args = ["--database", str(svc.store.path)]
    response = runner.invoke(app, [*args, "list"])
    assert response.exit_code == 0
    assert json.loads(response.stdout) == svc.store.list()
    log = Path(svc.store.get(rid)["directory"]) / "workflow.log"
    log.write_text("가" * 30000 + "END\n")
    response = runner.invoke(app, [*args, "logs", rid])
    assert response.exit_code == 0
    assert "END" in response.stdout
    assert response.stdout.count("가") == 30000


def test_cli_import_does_not_require_web_dependencies():
    code = """
import sys
class DenyWeb:
 def find_spec(self, fullname, *args):
  if fullname.split('.')[0] in {'fastapi','uvicorn','starlette'}:raise ImportError(fullname)
sys.meta_path.insert(0,DenyWeb())
from dev_orchestra.cli import app
assert app is not None
"""
    subprocess.run([sys.executable, "-c", code], check=True)
