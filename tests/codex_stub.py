"""Offline Codex executable support exercising the real workspace MCP transport."""

WORKSPACE_CLIENT = r"""
import hashlib, subprocess
from dev_orchestra.domain.models import WorkspaceResult, WorkspaceTestResult, ReviewResultV2, ReviewAssessment
if 'sandbox' in sys.argv:
    raise SystemExit(0)
_workspace = None
def tool(name, **arguments):
    global _workspace
    if _workspace is None:
        config = {sys.argv[i+1].split('=', 1)[0]: sys.argv[i+1].split('=', 1)[1]
                  for i, arg in enumerate(sys.argv[:-1]) if arg == '-c'}
        command = json.loads(config['mcp_servers.workspace.command'])
        args = json.loads(config['mcp_servers.workspace.args'])
        _workspace = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    _workspace.stdin.write(json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call',
        'params':{'name':name,'arguments':arguments}}) + '\n')
    _workspace.stdin.flush()
    response = json.loads(_workspace.stdout.readline())
    assert 'error' not in response, response
    result = response['result']
    assert not result.get('isError'), result
    return json.loads(result['content'][0]['text'])
def edit(file, content):
    path = Path(file)
    return tool('edit_file', file=file,
                expected_hash=hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None,
                content=content)
def finish_workspace(req, result):
    role = req['role']
    if role == 'tester':
        policy = tool('test_policy')
        state = req['context'].get('workspace_resume', {})
        if state.get('metadata_only'):
            value = {'evidence_id':state['test_evidence']['id'], 'selection':policy}
        else:
            arguments = {'command_ids':policy['included']}
            if policy.get('selector_version') == 2:
                available = policy.get('available_commands') or req['context']['test_commands']
                arguments = {'command_ids':list(available), 'test_plan':[
                    {'command_id':key,'files':[],
                     'reason':'The disposable fixture has one suite covering AC1; its complete suite verifies the required behavior.'}
                    for key, command in available.items() if command.get('kind','test') == 'test'
                ]}
            value = tool('run_tests', **arguments)
        result = WorkspaceTestResult(summary='실제 테스트 확인', test_evidence_id=value['evidence_id'],
            command_ids=value['selection']['included'], coverage=[Coverage(criterion_id='AC1',command_ids=['unit'])])
    elif role in {'developer','fixer'}:
        result = WorkspaceResult(summary='구현 확인', no_change_reason='현재 구현 확인')
    elif role == 'reviewer':
        result = ReviewResultV2(summary='독립 검증', assessments=[
            ReviewAssessment(issue_id=item['issue_id'],candidate_sha=req['input_sha'],status='resolved',evidence='현재 코드와 테스트 확인')
            for item in req['context'].get('open_issues', [])])
    if _workspace is not None:
        _workspace.stdin.close()
        assert _workspace.wait(timeout=10) == 0
    return result
"""
