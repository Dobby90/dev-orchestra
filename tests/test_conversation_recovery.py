"""Bound CLI resume IDs, metadata-only calls and accounting; no model requests."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_workspace import setup as setup
from test_workspace import start_session

from dev_orchestra.config import Codex
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.infrastructure.conversations import binding, session_id, thread_event
from dev_orchestra.infrastructure.files import write

THREAD = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


def test_explicit_resume_and_missing_session_fallback_preserve_usage(setup, tmp_path):
    session, request = start_session(setup)
    mode = tmp_path / "mode"
    mode.write_text("ok")
    stub = tmp_path / "codex-stub"
    stub.write_text(f"""#!{sys.executable}
import json,sys
from pathlib import Path
if 'sandbox' in sys.argv: raise SystemExit(0)
assert '--last' not in sys.argv
assert '--ephemeral' not in sys.argv
assert sys.argv[sys.argv.index('--sandbox')+1]=='read-only'
result=Path(sys.argv[sys.argv.index('--output-last-message')+1])
prompt=sys.stdin.read()
result.with_name('received-prompt.txt').write_text(prompt)
if 'resume' in sys.argv:
    assert sys.argv[-2]=={THREAD!r}
    if Path({str(mode)!r}).read_text()=='missing':
        print('session not found')
        raise SystemExit(1)
thread={THREAD!r} if Path({str(mode)!r}).read_text()=='ok' else {OTHER!r}
print(json.dumps({{'type':'thread.started','thread_id':thread}}),flush=True)
print(json.dumps({{'type':'turn.completed','usage':{{'input_tokens':10,'cached_input_tokens':3,'output_tokens':2}}}}),flush=True)
result.write_text(json.dumps({{'summary':'done','questions':[],'no_change_reason':'existing implementation satisfies requirements'}}))
""")
    stub.chmod(0o755)
    client = CodexClient(Codex(command=str(stub)))
    request.context["prd"] = "FULL_REQUIREMENTS_MARKER" * 3000
    client.run_workspace(request, session.path.parent, session)
    assert session_id(session, client.config.for_role("developer")) == THREAD
    session.reload()
    session.data.update(continuing=True, metadata_only=True)
    session.save()
    request.context["workspace_resume"] = {
        "metadata_only": True,
        "candidate_sha": session.git.sha(),
    }
    client.run_workspace(request, session.path.parent, session)
    assert (
        "FULL_REQUIREMENTS_MARKER"
        not in (session.path.parent / "calls/call-002/received-prompt.txt").read_text()
    )
    assert (
        "Repair only final response metadata"
        in (session.path.parent / "calls/call-002/received-prompt.txt").read_text()
    )
    mode.write_text("missing")
    client.run_workspace(request, session.path.parent, session)
    assert session_id(session, client.config.for_role("developer")) == OTHER
    telemetry = json.loads((session.path.parent / "telemetry.json").read_text())
    assert telemetry["usage"] == {"input_tokens": 30, "cached_input_tokens": 9, "output_tokens": 6}
    assert telemetry["completed_turns"] == 3 and telemetry["usage_status"] == "partial"
    assert len(list((session.path.parent / "calls").glob("call-*"))) == 4
    assert session.git.sha() == request.input_sha


def test_damaged_conversation_falls_back_but_other_run_is_rejected(setup):
    session, _ = start_session(setup)
    options = Codex().for_role("developer")
    thread_event(session, options)({"type": "thread.started", "thread_id": THREAD})
    path = session.path.parent / "conversation.json"
    path.write_text("damaged")
    assert session_id(session, options) is None
    run = session.store.get(session.run_id)
    refs = run["codex_conversations"]
    refs[str(session.data["step_id"])]["hash"] = write(path, [])
    session.store.update(session.run_id, codex_conversations=refs)
    assert session_id(session, options) is None
    body = {"session_id": OTHER, "binding": binding(session, options) | {"run_id": "another-run"}}
    sha = write(path, body)
    refs[str(session.data["step_id"])]["hash"] = sha
    session.store.update(session.run_id, codex_conversations=refs)
    with pytest.raises(PipelineError, match="another Run"):
        session_id(session, options)


@pytest.mark.skipif(os.getenv("DEVORCHESTRA_TEST_SANDBOX") != "1", reason="opt-in installed CLI")
def test_installed_cli_accepts_explicit_resume_with_policy_and_schema(tmp_path):
    result = subprocess.run(
        [
            "codex",
            "-a",
            "never",
            "exec",
            "--cd",
            str(tmp_path),
            "--sandbox",
            "read-only",
            "resume",
            "--ignore-user-config",
            "--ignore-rules",
            "--output-schema",
            str(tmp_path / "schema.json"),
            "--json",
            "--help",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0 and "--output-schema" in result.stdout


def test_resumed_call_logs_remain_in_role_log_queries(setup):
    from dev_orchestra.application.events import EventService

    svc, rid, _ = setup
    root = Path(svc.store.get(rid)["directory"])
    log = root / "iterations/000/developer/attempt-001/calls/call-002/process.log"
    log.parent.mkdir(parents=True)
    log.write_text("resumed call\n")
    assert EventService(svc.store).log_paths(rid, "developer") == [log]
    assert EventService(svc.store).log_paths(rid, "reviewer") == []
