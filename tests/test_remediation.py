import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests
from test_workflow import service as service

from dev_orchestra.config import Codex
from dev_orchestra.domain.models import AgentRequest, PipelineError
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.process import execute, redact
from dev_orchestra.infrastructure.store import Store
from dev_orchestra.infrastructure.test_runner import parse_junit
from dev_orchestra.orchestration.runner import Runner


def test_external_commit_cannot_be_approved(service):
    svc, rid, _ = service

    class External(Runner):
        def report(self, run_id):
            run = self.store.get(run_id)
            if run["state"] == "reporting":
                tree = Path(run["worktree"])
                (tree / "main.py").write_text("external change\n")
                Git(tree).commit("검증 후 변경 재현")
            super().report(run_id)

    External(svc.store, FakeAgent(), FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "failed"
    with pytest.raises(PipelineError):
        svc.approve(rid)


def test_cleanup_does_not_hide_changed_feature_ref(service):
    svc, rid, repo = service
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    svc.approve(rid)
    run = svc.store.get(rid)
    svc.cleanup(rid)
    new = Git(repo).commit("외부 변경")
    Git(repo).call("update-ref", "refs/heads/" + run["branch"], new)
    assert svc.status(rid)["state"] == "needs_input"


def test_approval_report_matches_state(service):
    svc, rid, _ = service
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    svc.approve(rid)
    assert "**state**: completed" in svc.report(rid)


def test_resume_detects_prior_evidence_corruption(service):
    svc, rid, _ = service

    class Failing(FakeAgent):
        def run(self, req, *args):
            if req.role == "reviewer":
                raise PipelineError("stop")
            return super().run(req, *args)

    Runner(svc.store, Failing(), FakeTests()).execute(rid)
    step = next(s for s in svc.store.steps(rid) if s["role"] == "tester")
    Path(step["data"]["artifact"]).write_text("modified")
    with pytest.raises(PipelineError, match="modified"):
        svc.resume(rid)


@pytest.mark.parametrize(
    "text",
    [
        '{"token":"CANARY"}',
        "password='CANARY'",
        "Authorization: Bearer CANARY",
        '{"Authorization":"Bearer CANARY"}',
        'const token = "CANARY";',
        "let token = 'CANARY';",
        "token=++CANARY",
        '{"token":"++CANARY"}',
        'const token = ++counter.current, password = "CANARY";',
    ],
)
def test_redacts_common_secret_formats(text):
    assert "CANARY" not in redact(text)


@pytest.mark.parametrize(
    "text",
    [
        " const token = ++fileRequest.current, inputVersion = version.current;",
        "+  let token = ++requestId;",
        "-  var token = --pending.current;",
    ],
)
def test_redaction_preserves_javascript_request_counters(text):
    assert redact(text) == text


def test_filter_execution_is_rejected(service, tmp_path):
    _, _, repo = service
    subprocess.run(
        ["git", "-C", str(repo), "config", "filter.audit.clean", f"touch {tmp_path}/forbidden"],
        check=True,
    )
    with pytest.raises(PipelineError, match="filters"):
        Git(repo).commit("필터 실행 금지")
    assert not (tmp_path / "forbidden").exists()


def test_incomplete_review_json_rejected(tmp_path):
    stub = tmp_path / "codex"
    stub.write_text(
        f'#!{sys.executable}\nimport sys\nfrom pathlib import Path\nPath(sys.argv[sys.argv.index("--output-last-message")+1]).write_text(\'{{"summary":"ok"}}\')\n'
    )
    stub.chmod(0o755)
    request = AgentRequest(
        run_id="r",
        role="reviewer",
        worktree=str(tmp_path),
        iteration=0,
        input_sha="sha",
        context={},
    )
    with pytest.raises(PipelineError, match="schema"):
        CodexClient(Codex(command=str(stub))).run(request, tmp_path / "output")


def test_preflight_timeout(tmp_path):
    stub = tmp_path / "codex"
    stub.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    stub.chmod(0o755)
    with pytest.raises(PipelineError, match="timeout"):
        CodexClient(Codex(command=str(stub), timeout_seconds=1)).check()


def test_suite_error_is_not_success(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text('<testsuite errors="1"><testcase name="ok"/></testsuite>')
    with pytest.raises(PipelineError):
        parse_junit(report)


def test_state_and_version_validation(tmp_path):
    store = Store(tmp_path / "state.db")
    store.create({"id": "r"})
    with pytest.raises(ValueError):
        store.update("r", "unknown")
    with pytest.raises(PipelineError):
        store.update("r", "completed")
    with pytest.raises(PipelineError):
        store.update("r", "planning", expected_version=10)
    with store.connection() as db:
        db.execute("UPDATE schema_version SET version=999")
    with pytest.raises(PipelineError, match="schema"):
        Store(store.path)


def test_control_events_have_cursor(service):
    svc, rid, _ = service
    svc.store.control(rid, "cancel")
    event = svc.store.events(rid)[-1]
    assert event["kind"] == "control_requested"
    assert json.loads(event["data"])["schema_version"] == 1
    assert svc.store.events(rid, event["id"]) == []


def test_parent_exit_does_not_leave_term_ignoring_child(tmp_path):
    pidfile = tmp_path / "pid"
    child = f"import signal,time,os;from pathlib import Path;signal.signal(signal.SIGTERM,signal.SIG_IGN);Path({str(pidfile)!r}).write_text(str(os.getpid()));time.sleep(60)"
    parent = f'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);time.sleep(60)'
    pid = None
    try:
        with pytest.raises(PipelineError):
            execute([sys.executable, "-c", parent], tmp_path, 1, tmp_path / "log")
        pid = int(pidfile.read_text())
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            pytest.fail("descendant survived timeout")
    finally:
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_queue_cancel_is_independent_of_execution_slot(service):
    import threading

    from dev_orchestra.infrastructure.locking import OwnershipError, lock

    svc, rid, _ = service
    directory = Path(svc.store.get(rid)["directory"])
    errors = []

    def execute_run():
        try:
            Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
        except Exception as error:
            errors.append(error)

    with lock(svc.store.path.with_suffix(".worker.lock")):
        thread = threading.Thread(target=execute_run)
        thread.start()
        for _ in range(100):
            try:
                with lock(directory / "execution.lock"):
                    pass
            except OwnershipError:
                break
            time.sleep(0.01)
        svc.cancel(rid)
        thread.join(3)
        assert not thread.is_alive()
        assert svc.store.get(rid)["state"] == "cancelled"
    assert not errors


def test_schema_v1_migration_preserves_data(tmp_path):
    store = Store(tmp_path / "state.db")
    store.create({"id": "legacy", "spec": "unchanged"})
    with store.connection() as db:
        db.execute("UPDATE schema_version SET version=1")
    migrated = Store(store.path)
    assert migrated.get("legacy")["spec"] == "unchanged"
    with migrated.connection() as db:
        assert db.execute("SELECT version FROM schema_version").fetchall()[0][0] == 2
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_preflight_failure_has_report(service):
    svc, rid, _ = service
    client = CodexClient(Codex(command="/nonexistent/devorchestra-codex"))
    Runner(svc.store, client, FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "failed"
    assert "nonexistent" in svc.report(rid)


def test_log_cursor_replay(service):
    svc, rid, _ = service
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    first = svc.log_chunks(rid)
    assert first["chunks"]
    assert svc.log_chunks(rid, first["cursor"])["chunks"] == []
    path = Path(svc.store.get(rid)["directory"]) / "workflow.log"
    with path.open("a") as f:
        f.write("additional\n")
    second = svc.log_chunks(rid, first["cursor"])
    assert second["chunks"][0]["text"] == "additional\n"


@pytest.mark.parametrize("boundary", ["commit", "checkpoint"])
def test_abrupt_exit_at_durable_boundaries(service, boundary, tmp_path):
    svc, rid, _ = service
    config = tmp_path / "config.json"
    config.write_text(svc.config.model_dump_json())
    code = (
        """
import os
from pathlib import Path
from test_workflow import FakeAgent, FakeTests
from dev_orchestra.config import Config
from dev_orchestra.application.service import RunService
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.store import Store
from dev_orchestra.orchestration.runner import Runner
from unittest.mock import patch
from dev_orchestra.domain.models import AgentResult
s=RunService(Config.model_validate_json(Path(CONFIG).read_text()))
class Changing(FakeAgent):
 def run_workspace(self,req,directory,session,stop):
  if req.role=='developer':
   from test_workspace import edit
   from dev_orchestra.domain.models import WorkspaceResult
   edit(session,'main.py','changed\\n')
   return WorkspaceResult(summary='구현 수정')
  return super().run_workspace(req,directory,session,stop)
original_commit=Git.commit_selected
original_finish=Store.finish_step
def commit(self,*args,**kwargs):
 value=original_commit(self,*args,**kwargs)
 if BOUNDARY=='commit':os._exit(17)
 return value
def finish(self,step,sha,data):
 original_finish(self,step,sha,data)
 if '/developer/' in data['artifact'] and BOUNDARY=='checkpoint':os._exit(17)
with patch.object(Git,'commit_selected',commit),patch.object(Store,'finish_step',finish):
 Runner(s.store,Changing(),FakeTests()).execute(RUN_ID)
""".replace("CONFIG", repr(str(config)))
        .replace("BOUNDARY", repr(boundary))
        .replace("RUN_ID", repr(rid))
    )
    env = os.environ | {
        "PYTHONPATH": str(Path("src").resolve()) + os.pathsep + str(Path("tests").resolve())
    }
    result = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=90
    )
    assert result.returncode == 17, result.stderr
    svc.resume(rid)
    agent = FakeAgent()
    Runner(svc.store, agent, FakeTests()).execute(rid)
    run = svc.store.get(rid)
    assert "planner" not in agent.calls
    assert (Path(run["worktree"]) / "main.py").read_text() == "changed\n"
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls == ["tester", "reviewer"]


def test_log_cursor_preserves_korean(service):
    svc, rid, _ = service
    path = Path(svc.store.get(rid)["directory"]) / "workflow.log"
    path.write_text("한글로그\n")
    text = ""
    cursor = {}
    for _ in range(20):
        result = svc.log_chunks(rid, cursor, limit=1)
        cursor = result["cursor"]
        text += "".join(c["text"] for c in result["chunks"])
        if not result["chunks"]:
            break
    assert text == "한글로그\n"


def test_concurrent_report_artifacts_are_atomic(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from dev_orchestra.infrastructure.files import write

    path = tmp_path / "report.json"
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda value: write(path, {"value": value}), range(32)))
    assert json.loads(path.read_text())["value"] in range(32)
    assert path.stat().st_mode & 0o077 == 0
    assert not list(tmp_path.glob(".report.json.*"))
