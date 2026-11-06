"""Real Worker death tests; no model calls or user repository mutations."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest
from codex_stub import WORKSPACE_CLIENT
from test_workflow import service as service

from dev_orchestra.application.service import RunService
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.process import execution_scope
from dev_orchestra.infrastructure.process_owner import alive, identity, markers


def until(check, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("lifecycle probe timed out")


@pytest.fixture
def launch(service, tmp_path, monkeypatch):
    original_service, original_id, repo = service
    processes = []
    workers = []

    def start(stage="developer"):
        release = tmp_path / (stage + "-release")
        stub = tmp_path / (stage + "-codex")
        stub.write_text(f"""#!{sys.executable}
import json,sys,time
from pathlib import Path
{WORKSPACE_CLIENT}
from dev_orchestra.domain.models import AgentResult,Criterion,Coverage
if '--help' in sys.argv:
 if {stage!r}=='preflight':
  while not Path({str(release)!r}).exists():time.sleep(.05)
 print('--output-schema --ephemeral --ignore-user-config --json');sys.exit(0)
data=json.loads(sys.stdin.read().split('UNTRUSTED TASK DATA:\\n',1)[1]);role=data['role']
if role=={stage!r}:
 while not Path({str(release)!r}).exists():time.sleep(.05)
result=AgentResult(summary='검증')
if role=='planner':result.plan='계획';result.criteria=[Criterion(id='AC1',description='조건')]
if role=='tester':result.command_ids=['unit'];result.coverage=[Coverage(criterion_id='AC1',command_ids=['unit'])]
result=finish_workspace(data,result)
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(result.model_dump_json())
""")
        stub.chmod(0o755)
        config = original_service.config.model_copy(deep=True)
        config.codex.command = str(stub)
        # The configured command runs in the actual OS test sandbox.
        config.tests.commands["unit"].argv = [
            sys.executable,
            "-c",
            "from pathlib import Path;Path('build/results.xml').write_text('<testsuite><testcase name=\"ok\"/></testsuite>')",
        ]
        if stage == "test_command":
            config.tests.commands["unit"].argv = [
                sys.executable,
                "-c",
                "import time;time.sleep(60)",
            ]
        svc = RunService(config)
        rid = svc.create(
            repo, Path(original_service.store.get(original_id)["directory"]) / "spec.md"
        )
        git_started = tmp_path / "git-started"
        if stage == "git_worktree":
            real_git = shutil.which("git")
            wrapper = tmp_path / "bin" / "git"
            wrapper.parent.mkdir()
            wrapper.write_text(f"""#!{sys.executable}
import os,sys,time
from pathlib import Path
if 'worktree' in sys.argv and 'add' in sys.argv:
 Path({str(git_started)!r}).write_text(str(os.getpid()));time.sleep(60)
os.execv({real_git!r},[{real_git!r},*sys.argv[1:]])
""")
            wrapper.chmod(0o755)
            monkeypatch.setenv("PATH", str(wrapper.parent) + os.pathsep + os.environ["PATH"])
        worker = svc.start(rid, detached=True)
        workers.append(worker)
        directory = Path(svc.store.get(rid)["directory"])

        def command_started():
            pattern = (
                "processes/*.json"
                if stage in {"preflight", "git_worktree"}
                else "iterations/*/tester/*/test-*/unit.process.json"
                if stage == "test_command"
                else f"iterations/*/{stage}/*/**/process.process.json"
            )
            for mirror in directory.glob(pattern):
                record = json.loads(mirror.read_text())
                if record.get("pid") and not record["finished"]:
                    if stage == "preflight":
                        try:
                            if str(stub) not in psutil.Process(record["pid"]).cmdline():
                                continue
                        except psutil.NoSuchProcess:
                            continue
                    if stage == "git_worktree" and (
                        not git_started.exists() or record["pid"] != int(git_started.read_text())
                    ):
                        continue
                    return record
            return None

        # Preparing workspace snapshots and independent roles can exceed thirty seconds under
        # concurrent native/browser load. Wait for the actual injection boundary;
        # post-kill termination deadlines and all ownership assertions stay strict.
        record = until(command_started, timeout=90)
        processes.append(record)
        return svc, rid, worker, record, release

    yield start
    # Safety net cleans only records belonging to test-owned child identities.
    for record in processes:
        for label in [record, record.get("guardian")]:
            if label and alive(label):
                process = psutil.Process(label["pid"])
                if process.create_time() == label["create_time"]:
                    process.kill()
    for pid in workers:
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def kill_worker(pid):
    os.kill(pid, signal.SIGKILL)
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


@pytest.mark.parametrize(
    "stage", ["preflight", "planner", "developer", "tester", "test_command", "git_worktree"]
)
def test_worker_sigkill_then_cancel_confirms_child_exit(launch, stage):
    svc, rid, worker, child, _ = launch(stage)
    repo = Path(svc.store.get(rid)["repo"])
    sha, source = Git(repo).sha(), (repo / "main.py").read_bytes()
    kill_worker(worker)
    assert svc.status(rid)["state"] == "failed"
    svc.cancel(rid)
    assert svc.status(rid)["state"] == "cancelled"
    assert not alive(child)
    records = list(markers(Path(svc.store.get(rid)["directory"])))
    assert records and all(r["finished"] and r["termination_confirmed"] for _, r in records)
    assert Git(repo).sha() == sha and (repo / "main.py").read_bytes() == source


def test_guardian_cleans_up_without_status_or_cancel(launch):
    svc, rid, worker, child, _ = launch()
    kill_worker(worker)
    until(lambda: not alive(child))
    # Cleanup is driven by the kernel closing the Worker pipe, not GUI polling.
    records = until(lambda: list(markers(Path(svc.store.get(rid)["directory"]))))
    until(
        lambda: all(
            r["termination_confirmed"] for _, r in markers(Path(svc.store.get(rid)["directory"]))
        )
    )
    assert records


def test_worker_sigkill_then_resume_preserves_planner(launch):
    svc, rid, worker, child, release = launch()
    before = next(s for s in svc.store.steps(rid) if s["role"] == "developer")
    kill_worker(worker)
    until(lambda: not alive(child))
    release.touch()
    svc.resume(rid)
    svc.start(rid)
    assert svc.status(rid)["state"] == "waiting_approval"
    steps = svc.store.steps(rid)
    assert len([s for s in steps if s["role"] == "planner"]) == 1
    developers = [s for s in steps if s["role"] == "developer"]
    assert len(developers) == 1 and developers[0]["status"] == "completed"
    assert (developers[0]["id"], developers[0]["attempt"]) == (before["id"], before["attempt"])
    svc.approve(rid)
    assert svc.status(rid)["state"] == "completed"


def test_duplicate_worker_does_not_replace_owner(launch):
    svc, rid, worker, child, _ = launch()
    directory = Path(svc.store.get(rid)["directory"])
    original_owner = json.loads((directory / "worker.json").read_text())
    svc.start(rid, detached=True)
    assert svc.status(rid)["state"] == "developing"
    assert json.loads((directory / "worker.json").read_text()) == original_owner
    assert alive(child)
    svc.cancel(rid)
    until(lambda: svc.status(rid)["state"] == "cancelled")
    assert not alive(child)


def test_reused_guardian_pid_never_authorizes_signals(service, tmp_path):
    svc, rid, _ = service
    unrelated = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        guardian = identity(unrelated.pid)
        guardian["create_time"] -= 100
        directory = Path(svc.store.get(rid)["directory"])
        write(
            directory / "processes" / ("a" * 32 + ".json"),
            {"protocol": 2, "token": "a" * 32, "finished": False, "guardian": guardian},
        )
        with pytest.raises(PipelineError, match="guardian unavailable"):
            svc.cancel(rid)
        assert svc.store.get(rid)["state"] == "needs_input"
        assert unrelated.poll() is None
        assert not list((directory / "processes").glob("*.control.json"))
    finally:
        unrelated.kill()
        unrelated.wait()


def test_dead_guardian_cannot_report_false_cancellation(launch):
    svc, rid, worker, child, _ = launch()
    guard = psutil.Process(child["guardian"]["pid"])
    assert guard.create_time() == child["guardian"]["create_time"]
    guard.kill()
    kill_worker(worker)
    with pytest.raises(PipelineError, match="guardian unavailable"):
        svc.cancel(rid)
    assert svc.store.get(rid)["state"] == "needs_input"
    assert alive(child)


def test_live_worker_cannot_acknowledge_cancel_after_guardian_loss(launch):
    svc, rid, _, child, _ = launch()
    guard = psutil.Process(child["guardian"]["pid"])
    assert guard.create_time() == child["guardian"]["create_time"]
    guard.kill()
    until(lambda: not alive(child["guardian"]))
    svc.cancel(rid)
    until(lambda: svc.status(rid)["state"] == "needs_input")
    assert alive(child)
    assert svc.store.get(rid)["state"] != "cancelled"


@pytest.mark.parametrize("control, expected", [("cancel", "cancelled"), ("pause", "paused")])
def test_pending_control_after_worker_completion_is_acknowledged(service, control, expected):
    from test_workflow import FakeAgent, FakeTests

    from dev_orchestra.orchestration.runner import Runner

    svc, rid, _ = service
    Runner(svc.store, FakeAgent(), FakeTests()).execute(rid)
    assert svc.store.get(rid)["state"] == "waiting_approval"
    # A client/Worker can disappear after durable request creation but before ack.
    svc.store.control(rid, control)
    assert svc.status(rid)["state"] == expected
    events = svc.store.events(rid)
    assert svc.status(rid)["state"] == expected
    assert svc.store.events(rid) == events


def test_guarded_git_preserves_diff_but_redacts_persisted_logs(service):
    svc, rid, repo = service
    directory = Path(svc.store.get(rid)["directory"])
    secret = "sk-lifecycleTestSecret123"
    (repo / "main.py").write_text(f"api_key = '{secret}'\n")
    with execution_scope(directory):
        diff = Git(repo).call("diff", "--no-ext-diff")
    assert secret in diff
    assert secret not in "".join(p.read_text() for p in (directory / "git").glob("*.log"))
    assert all(record["termination_confirmed"] for _, record in markers(directory))


def test_real_worker_pipeline_fixes_failing_pytest_and_approves(service, tmp_path):
    base_service, base_id, repo = service
    test_content = "from pathlib import Path\nimport runpy\ndef test_value():\n    assert runpy.run_path(str(Path(__file__).resolve().parents[1] / 'main.py'))['value'] == 2\n"
    stub = tmp_path / "pipeline-codex"
    stub.write_text(f"""#!{sys.executable}
import json,sys
from pathlib import Path
{WORKSPACE_CLIENT}
from dev_orchestra.domain.models import AgentResult,Criterion,Coverage
if '--help' in sys.argv:
 print('--output-schema --ephemeral --ignore-user-config --json');sys.exit(0)
req=json.loads(sys.stdin.read().split('UNTRUSTED TASK DATA:\\n',1)[1]);role=req['role']
result=AgentResult(summary='검증')
if role=='planner':result.plan='값 2 구현';result.criteria=[Criterion(id='AC1',description='value == 2')]
if role=='tester':
 result.command_ids=['unit'];result.coverage=[Coverage(criterion_id='AC1',command_ids=['unit'])]
 if not Path('tests/test_value.py').exists():edit('tests/test_value.py',{test_content!r})
if role=='fixer':edit('main.py','value = 2\\n')
result=finish_workspace(req,result)
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(result.model_dump_json())
""")
    stub.chmod(0o755)
    config = base_service.config.model_copy(deep=True)
    config.codex.command = str(stub)
    config.tests.commands["unit"].argv = [
        sys.executable,
        "-m",
        "pytest",
        "tests",
        "--junitxml=build/results.xml",
        "-q",
    ]
    svc = RunService(config)
    rid = svc.create(repo, Path(base_service.store.get(base_id)["directory"]) / "spec.md")
    sha, source = Git(repo).sha(), (repo / "main.py").read_bytes()
    svc.start(rid)
    run = svc.status(rid)
    assert run["state"] == "waiting_approval", run.get("error")
    assert run["iteration"] == 1
    steps = svc.store.steps(rid)
    tests = [
        json.loads(Path(s["data"]["artifact"]).read_text())
        for s in steps
        if s["role"] == "tester" and s["status"] == "completed"
    ]
    assert tests[0]["execution"]["commands"][0]["exit_code"] != 0
    assert tests[1]["execution"]["commands"][0]["exit_code"] == 0
    assert run["final_sha"] == run["test_result"]["sha"] == run["review_result"]["sha"]
    svc.approve(rid)
    assert svc.status(rid)["state"] == "completed"
    assert Git(repo).sha() == sha and (repo / "main.py").read_bytes() == source
    assert all(record["termination_confirmed"] for _, record in markers(Path(run["directory"])))
