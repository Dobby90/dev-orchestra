"""Real stdio host tool traffic, driven by a CLI stub without any model call."""

import json
import sys

from test_workspace import setup as setup
from test_workspace import start_session

from dev_orchestra.config import Codex
from dev_orchestra.domain.models import AgentRequest
from dev_orchestra.infrastructure import workspace_mcp
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.workspace import ReviewEvidenceSession


def stub(tmp_path, role, evidence_id=None):
    path = tmp_path / ("stub-" + role)
    path.write_text(f"""#!{sys.executable}
import json, subprocess, sys
from pathlib import Path
if 'sandbox' in sys.argv:
    raise SystemExit(0)
assert sys.argv[sys.argv.index('--sandbox')+1] == 'read-only'
schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema')+1]).read_text())
assert 'patch' not in schema['properties']
config = {{sys.argv[i+1].split('=',1)[0]: sys.argv[i+1].split('=',1)[1]
          for i,arg in enumerate(sys.argv[:-1]) if arg == '-c'}}
command = json.loads(config['mcp_servers.workspace.command'])
args = json.loads(config['mcp_servers.workspace.args'])
proc = subprocess.Popen([command,*args], stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
def call(method,params=None,denied=False):
    proc.stdin.write(json.dumps({{'jsonrpc':'2.0','id':1,'method':method,'params':params or {{}}}})+'\\n')
    proc.stdin.flush()
    response=json.loads(proc.stdout.readline())
    if denied:
        assert response.get('error',{{}}).get('message')=='unsupported workspace tool',response
        return
    assert 'error' not in response,response
    return response['result']
call('initialize')
tools={{tool['name'] for tool in call('tools/list')['tools']}}
assert set(json.loads(config['mcp_servers.workspace.enabled_tools']))==tools
if {role!r} == 'tester':
    assert tools == {{'edit_file','run_tests','test_policy','read_test_evidence','workspace_state'}}
    state=call('tools/call',{{'name':'workspace_state','arguments':{{'paths':['tests/test_main.py']}}}})
    files=json.loads(state['content'][0]['text'])
    assert set(files) == {{'candidate_sha','files'}} and set(files['files']) == {{'tests/test_main.py'}}
    result=call('tools/call',{{'name':'run_tests','arguments':{{'command_ids':['unit']}}}})
    assert not result.get('isError'),result
    value=json.loads(result['content'][0]['text'])
    assert value['execution']['status']=='passed'
    target={{'evidence_id':value['evidence_id'],'command_id':'unit'}}
    def read(**changes):
        return call('tools/call',{{'name':'read_test_evidence','arguments':target|changes}})
    result=read()
    command=json.loads(result['content'][0]['text'])
    assert command['command']['status']=='passed' and command['sha']==files['candidate_sha']
    assert command['command']['junit_hash'] and json.loads(command['text'])['failure_details']==[]
    assert read(view='diagnostics',offset=0,limit=8000)==result
    first=json.loads(read(limit=20)['content'][0]['text'])
    second=json.loads(read(offset=20,limit=20)['content'][0]['text'])
    assert first['truncated'] and first['text']+second['text']==command['text'][:40]
    named=json.loads(read(view='test_cases')['content'][0]['text'])
    assert json.loads(named['text'])['test_cases']==['one']
    assert read(evidence_id='private-invalid-argument')['isError']
    response={{'summary':'실제 테스트 결과 확인','questions':[],'no_change_reason':'',
               'command_ids':['unit'],'coverage':[{{'criterion_id':'AC1','command_ids':['unit']}}],
               'test_evidence_id':value['evidence_id']}}
else:
    assert tools == {{'read_test_evidence'}}
    result=call('tools/call',{{'name':'read_test_evidence','arguments':{{'evidence_id':{evidence_id!r},'limit':500}}}})
    assert not result.get('isError'),result
    page=json.loads(result['content'][0]['text'])
    text=page['text']
    while page['truncated']:
        result=call('tools/call',{{'name':'read_test_evidence','arguments':{{'evidence_id':{evidence_id!r},'offset':len(text)}}}})
        assert not result.get('isError'),result
        page=json.loads(result['content'][0]['text'])
        text+=page['text']
    evidence=json.loads(text)
    assert evidence['coverage']==[{{'criterion_id':'AC1','command_ids':['unit']}}]
    assert evidence['execution']['passed'] and evidence['revision']==0
    for name in ('edit_file','run_tests'):
        call('tools/call',{{'name':name,'arguments':{{}}}},denied=True)
    response={{'summary':'독립 리뷰 완료','questions':[],'issues':[]}}
proc.stdin.close()
assert proc.wait(timeout=10)==0
sys.stdin.read()
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(response))
""")
    path.chmod(0o755)
    return CodexClient(Codex(command=str(path)))


def test_tester_metadata_and_actual_tool_execution_over_stdio(setup, tmp_path):
    svc, rid, _ = setup
    config = svc.store.get(rid)["config"]
    code = "from pathlib import Path;Path('build/results.xml').write_text('<testsuite><testcase name=\"one\"/></testsuite>')"
    config["tests"]["commands"]["unit"]["argv"] = [sys.executable, "-c", code]
    svc.store.update(rid, config=config)
    session, request = start_session(setup, "tester")
    response = stub(tmp_path, "tester").run_workspace(request, session.path.parent, session)
    result, sha, _ = session.finish(response)
    assert result.command_ids == ["unit"] and sha == request.input_sha
    assert session.load_test_result(response)["execution"]["passed"]
    metrics_text = (session.path.parent / "tool-metrics.json").read_text()
    metrics = json.loads(metrics_text)["read_test_evidence"]
    assert metrics["calls"] == 6 and metrics["failed_calls"] == 1
    assert metrics["argument_bytes"] > 0 and metrics["result_bytes"] > 0
    pages = metrics["pages"]
    assert [
        (p["target"]["view"], p["target"]["offset"], p["target"]["limit"], p["calls"])
        for p in pages
    ] == [
        ("diagnostics", 0, 8000, 2),
        ("diagnostics", 0, 20, 1),
        ("diagnostics", 20, 20, 1),
        ("test_cases", 0, 8000, 1),
    ]
    assert all(p["target"]["evidence_id"] == response.test_evidence_id for p in pages)
    assert all(p["target"]["command_id"] == "unit" for p in pages)
    assert all(p["duration_seconds"] >= 0 for p in pages)
    assert metrics["duration_seconds"] >= sum(p["duration_seconds"] for p in pages)
    assert all(value not in metrics_text for value in ["private-invalid-argument", "junit_hash"])


def test_tester_evidence_metrics_keep_legacy_counts_and_bound_distinct_pages(monkeypatch):
    monkeypatch.setattr(workspace_mcp, "MAX_TRACKED_EVIDENCE_PAGES", 2)
    metrics = {"calls": 5, "argument_bytes": 10, "result_bytes": 20}
    for evidence_id in ("test-first", "test-second", "test-third", "test-first"):
        workspace_mcp.observe_evidence_read(metrics, {"evidence_id": evidence_id}, False, 0.25)

    assert metrics["calls"] == 5
    assert metrics["duration_seconds"] == 1
    assert metrics["untracked_page_reads"] == 1
    assert [
        (p["target"]["evidence_id"], p["calls"], p["duration_seconds"]) for p in metrics["pages"]
    ] == [
        ("test-first", 2, 0.5),
        ("test-second", 1, 0.25),
    ]


def test_reviewer_stdio_exposes_only_readonly_candidate_evidence(setup, tmp_path):
    from test_workflow import FakeTests

    svc, rid, _ = setup
    session, _ = start_session(setup, "tester")
    session._test_runner = FakeTests()
    value = session.run_tests(["unit"])
    artifact = session.path.parent / "result.json"
    checksum = write(
        artifact,
        {
            "sha": value["sha"],
            "revision": 0,
            "coverage": [{"criterion_id": "AC1", "command_ids": ["unit"]}],
            "execution": session.read_test_payload()["execution"],
            "test_selection": value["selection"],
        },
    )
    svc.store.finish_step(
        session.data["step_id"], value["sha"], {"artifact": str(artifact), "hash": checksum}
    )
    svc.store.update(rid, "reviewing", next_role="reviewer")
    step_id, _ = svc.store.start_step(rid, "reviewer", 0, value["sha"])
    readonly = ReviewEvidenceSession.create(
        svc.store, svc.store.get(rid), step_id, session.path.parent / "review"
    )
    request = AgentRequest(
        run_id=rid,
        role="reviewer",
        worktree=str(session.root),
        input_sha=value["sha"],
        iteration=0,
        context={},
    )
    result = stub(tmp_path, "reviewer", readonly.data["evidence_id"]).run_readonly(
        request, readonly.path.parent, readonly
    )
    assert result.summary == "독립 리뷰 완료"
    assert session.git.sha() == value["sha"]
