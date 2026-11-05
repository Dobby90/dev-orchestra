"""Real stdio host tool traffic, driven by a CLI stub without any model call."""

import sys

from test_workspace import setup as setup
from test_workspace import start_session

from dev_orchestra.config import Codex
from dev_orchestra.domain.models import AgentRequest
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
def call(method,params=None):
    proc.stdin.write(json.dumps({{'jsonrpc':'2.0','id':1,'method':method,'params':params or {{}}}})+'\\n')
    proc.stdin.flush()
    response=json.loads(proc.stdout.readline())
    assert 'error' not in response,response
    return response['result']
call('initialize')
tools={{tool['name'] for tool in call('tools/list')['tools']}}
if {role!r} == 'tester':
    assert tools == {{'edit_file','run_tests','test_policy','read_test_evidence'}}
    result=call('tools/call',{{'name':'run_tests','arguments':{{'command_ids':['unit']}}}})
    assert not result.get('isError'),result
    value=json.loads(result['content'][0]['text'])
    assert value['execution']['status']=='passed'
    response={{'summary':'실제 테스트 결과 확인','questions':[],'no_change_reason':'',
               'command_ids':['unit'],'coverage':[{{'criterion_id':'AC1','command_ids':['unit']}}],
               'test_evidence_id':value['evidence_id']}}
else:
    assert tools == {{'read_test_evidence'}}
    result=call('tools/call',{{'name':'read_test_evidence','arguments':{{'evidence_id':{evidence_id!r},'limit':500}}}})
    assert not result.get('isError'),result
    assert 'execution' in json.loads(result['content'][0]['text'])['text']
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
