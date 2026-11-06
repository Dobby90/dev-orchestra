"""workspace-v2 stage boundaries, real Git checkpoints and read-only transport."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_workflow import FakeAgent, FakeTests

from dev_orchestra.application.service import RunService
from dev_orchestra.config import Config
from dev_orchestra.domain.execution import WORKSPACE_V2, execution_contract
from dev_orchestra.domain.models import (
    AgentRequest,
    Coverage,
    PipelineError,
    WorkspaceResult,
    WorkspaceTestResult,
)
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.infrastructure.files import digest, write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.workspace import WorkspaceSession
from dev_orchestra.infrastructure.workspace_mcp import dispatch
from dev_orchestra.orchestration.runner import Runner


@pytest.fixture
def setup(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git = Git(repo)
    git.call("config", "user.name", "Test")
    git.call("config", "user.email", "test@example.invalid")
    (repo / "main.py").write_text("original\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_main.py").write_text("assert 1 == 1\n")
    (repo / ".gitignore").write_text("ignored.py\n")
    git.commit("초기 코드")
    (repo / "main.py").write_text("original local changes\n")
    spec = tmp_path / "prd.md"
    spec.write_text("Implement greeting")
    config = Config.model_validate(
        {
            "storage": {
                "database": tmp_path / "state.db",
                "runs_directory": tmp_path / "runs",
                "worktrees_directory": tmp_path / "worktrees",
            },
            "tests": {
                "commands": {"unit": {"argv": ["pytest"], "junit_report": "build/results.xml"}}
            },
        }
    )
    svc = RunService(config)
    from dev_orchestra.infrastructure.migrations import upgrade

    upgrade(svc.store.path, workspace=True)
    run_id = svc.create(repo, spec)
    return svc, run_id, repo


def start_session(setup, role="developer"):
    svc, run_id, _ = setup
    run = svc.store.get(run_id)
    git = Git(Path(run["repo"]))
    root = Path(run["worktree"])
    git.create(root, run["branch"], run["base_sha"])
    svc.store.update(
        run_id,
        "testing" if role == "tester" else "developing" if role == "developer" else "fixing",
        next_role=role,
        current_sha=run["base_sha"],
        criteria=[{"id": "AC1", "description": "new behavior"}],
    )
    step_id, _ = svc.store.start_step(run_id, role, 0, run["base_sha"])
    request = AgentRequest(
        run_id=run_id,
        role=role,
        worktree=str(root),
        iteration=0,
        input_sha=run["base_sha"],
        context={"execution_contract": WORKSPACE_V2},
    )
    session = WorkspaceSession.create(
        svc.store, svc.store.get(run_id), request, step_id, Path(run["directory"]) / "attempt"
    )
    return session, request


def edit(session, path, content, **kwargs):
    file = session.root / path
    return session.edit_file(
        path, digest(file.read_bytes()) if file.exists() else None, content, **kwargs
    )


def checkpoint(session, request_id, files):
    return session.checkpoint_commit(
        request_id,
        session.git.sha(),
        {name: digest((session.root / name).read_bytes()) for name in files},
        "기능 구현 " + request_id,
        "독립 작업 단위",
    )


@pytest.mark.parametrize("role", ["developer", "fixer"])
def test_multiple_checkpoints_selected_files_and_cumulative_result(setup, role):
    session, request = start_session(setup, role)
    edit(session, "main.py", "first\n")
    edit(session, "ignored.py", "new ignored source\n")
    first = checkpoint(session, "first", ["main.py"])
    assert session.git.call("show", "--format=", "--name-only", first["sha"]) == "main.py"
    assert "ignored.py" not in session.git.call("ls-tree", "--name-only", first["sha"])
    run = setup[0].store.get(request.run_id)
    assert run["next_role"] == role and run["iteration"] == 0
    assert run["current_sha"] == first["sha"]
    assert setup[0].store.steps(request.run_id)[0]["status"] == "running"
    second = checkpoint(session, "second", ["ignored.py"])
    assert second["parent"] == first["sha"] and first["parent"] == request.input_sha
    result, sha, commits = session.finish(WorkspaceResult(summary="완료"))
    assert sha == second["sha"] and len(commits) == 2
    assert "main.py" in result.patch and "ignored.py" in result.patch
    assert session.git.call("rev-list", "--count", request.input_sha + "..HEAD") == "2"
    assert (setup[2] / "main.py").read_text() == "original local changes\n"


def test_auto_final_commit_and_ignored_file_after_gitignore_edit(setup):
    session, _ = start_session(setup)
    edit(session, ".gitignore", "*.py\n")
    edit(session, "new.py", "new\n")
    result, sha, commits = session.finish(WorkspaceResult(summary="완료"))
    assert "new.py" in result.patch and len(commits) == 1
    assert session.git.call("show", sha + ":new.py") == "new"


def test_duplicate_request_and_lost_commit_response(setup, monkeypatch):
    session, _ = start_session(setup)
    edit(session, "main.py", "first\n")
    args = ("unit", session.git.sha(), {"main.py": digest(b"first\n")}, "단위 구현", "작업 요약")
    commit = session.git.commit_selected

    def lose_response(*args):
        commit(*args)
        raise PipelineError("lost response")

    monkeypatch.setattr(session.git, "commit_selected", lose_response)
    with pytest.raises(PipelineError, match="lost response"):
        session.checkpoint_commit(*args)
    monkeypatch.setattr(session.git, "commit_selected", commit)
    first = session.checkpoint_commit(*args)
    edit(session, "second.py", "second\n")
    second = checkpoint(session, "second", ["second.py"])
    assert session.checkpoint_commit(*args) == first
    assert session.git.sha() == second["sha"]
    assert setup[0].store.get(session.run_id)["current_sha"] == second["sha"]
    assert session.git.call("rev-list", "--count", session.data["input_sha"] + "..HEAD") == "2"
    with pytest.raises(PipelineError, match="reused"):
        session.checkpoint_commit(*args[:-1], "다른 요약")


@pytest.mark.parametrize(
    "file",
    [
        "../repo/main.py",
        "/tmp/outside",
        ".git",
        ".git/config",
        "nested/.git/config",
        ".env",
        "secret.pem",
        "build/hidden.py",
    ],
)
def test_denied_edit_paths(setup, file):
    session, _ = start_session(setup)
    with pytest.raises(PipelineError):
        session.edit_file(file, None, "invalid\n")


def test_existing_test_cas_evidence_and_binary_protection(setup):
    session, _ = start_session(setup)
    with pytest.raises(PipelineError, match="checksum"):
        session.edit_file("main.py", "wrong", "new\n")
    with pytest.raises(PipelineError, match="evidence"):
        edit(session, "tests/test_main.py", "assert 2 == 2\n")
    with pytest.raises(PipelineError, match="criterion"):
        edit(
            session,
            "tests/test_main.py",
            "assert 2 == 2\n",
            test_change={
                "file": "tests/test_main.py",
                "criterion_ids": ["invalid"],
                "reason": "changed expectation",
            },
        )
    with pytest.raises(PipelineError, match="binary"):
        edit(session, "main.py", "binary\0data")
    with pytest.raises(PipelineError, match="sensitive"):
        edit(session, "main.py", 'password = "actual-secret"')
    evidence = {
        "file": "tests/test_main.py",
        "criterion_ids": ["AC1"],
        "reason": "1에서 2로 요구사항 변경",
    }
    edit(session, "tests/test_main.py", "assert 2 == 2\n", test_change=evidence)
    result, _, commits = session.finish(WorkspaceResult(summary="테스트 기대값 수정"))
    assert result.test_changes[0].model_dump() == evidence
    assert commits[0]["test_changes"] == [evidence]


@pytest.mark.parametrize(
    "attack", ["direct", "ignored", "mode", "delete", "symlink", "staged", "branch"]
)
def test_bypass_is_detected_and_preserved(setup, attack):
    session, _ = start_session(setup)
    file = session.root / "main.py"
    if attack == "direct":
        file.write_text("bypass\n")
    elif attack == "ignored":
        (session.root / "ignored.py").write_text("bypass\n")
    elif attack == "mode":
        file.chmod(0o755)
    elif attack == "delete":
        file.unlink()
    elif attack == "symlink":
        file.unlink()
        file.symlink_to(setup[2] / "main.py")
    elif attack == "staged":
        file.write_text("bypass\n")
        session.git.call("add", "main.py")
    elif attack == "branch":
        session.git.call("checkout", "-b", "other")
    with pytest.raises(PipelineError):
        session.finish(WorkspaceResult(summary="부정확한 성공 선언"))
    assert session.git.sha() == session.data["input_sha"]


def test_output_paths_cannot_hide_source(setup):
    svc, run_id, _ = setup
    run = svc.store.get(run_id)
    config = run["config"]
    config["tests"]["output_paths"].append("main.py")
    svc.store.update(run_id, config=config)
    with pytest.raises(PipelineError, match="hide tracked"):
        start_session(setup)


@pytest.mark.parametrize("contract", [None, "patch-v1", "unknown"])
def test_historical_or_missing_contract_is_not_executable(setup, contract):
    with pytest.raises(PipelineError, match="unsupported"):
        execution_contract({} if contract is None else {"execution_contract": contract})
    svc, _, repo = setup
    spec = Path(svc.config.storage.runs_directory).parent / "prd.md"
    run_id = RunService(svc.config).create(repo, spec)
    assert svc.store.get(run_id)["execution_contract"] == WORKSPACE_V2


def test_runner_keeps_plan_approval_and_fix_loop(setup):
    svc, run_id, repo = setup

    class Agent(FakeAgent):
        def run_workspace(self, req, directory, session, stop):
            self.calls.append(req.role)
            if req.role == "tester":
                value = session.run_tests(session.test_policy()["included"])
                return WorkspaceTestResult(
                    summary="실제 테스트 확인",
                    test_evidence_id=value["evidence_id"],
                    command_ids=value["selection"]["included"],
                    coverage=[Coverage(criterion_id="AC1", command_ids=["unit"])],
                )
            edit(session, "main.py", req.role + "\n")
            edit(
                session,
                "tests/test_main.py",
                "assert " + repr(req.role) + "\n",
                test_change={
                    "file": "tests/test_main.py",
                    "criterion_ids": ["AC1"],
                    "reason": "요구사항에 맞게 역할별 기대값을 변경합니다",
                },
            )
            checkpoint(session, "first", ["main.py", "tests/test_main.py"])
            edit(session, req.role + ".py", "new\n")
            checkpoint(session, "second", [req.role + ".py"])
            return WorkspaceResult(summary="파일 직접 구현 완료")

    agent = Agent(block=1)
    Runner(svc.store, agent, FakeTests()).execute(run_id)
    assert svc.store.get(run_id)["state"] == "waiting_plan_approval"
    assert agent.calls == ["planner"]
    # Use the actual plan approval service, preserving its SHA/revision bindings.
    from dev_orchestra.application.plan_approval import binding, decide

    run = svc.store.get(run_id)
    decide(svc, run_id, binding(svc.store, run)["plan_hash"], True)
    Runner(svc.store, agent, FakeTests()).execute(run_id)
    run = svc.store.get(run_id)
    assert run["state"] == "waiting_approval", run.get("error")
    assert agent.calls == [
        "planner",
        "developer",
        "tester",
        "reviewer",
        "fixer",
        "tester",
        "reviewer",
    ]
    for step in svc.store.steps(run_id):
        if step["role"] in {"developer", "fixer"}:
            payload = json.loads(Path(step["data"]["artifact"]).read_text())
            assert len(payload["workspace_commits"]) == 2
            assert payload["workspace_commits"][0]["parent"] == step["input_sha"]
            assert payload["sha"] == step["output_sha"]
            assert "main.py" in payload["patch"] and step["role"] + ".py" in payload["patch"]
    svc.approve(run_id)
    assert svc.store.get(run_id)["state"] == "completed"
    assert (repo / "main.py").read_text() == "original local changes\n"


def test_mcp_dispatch_rejects_extra_fields_and_arbitrary_tools(setup):
    session, _ = start_session(setup)
    definitions = dispatch(session, {"method": "tools/list"})
    assert {tool["name"] for tool in definitions["tools"]} == {
        "edit_file",
        "checkpoint_commit",
        "workspace_state",
    }
    result = dispatch(
        session,
        {
            "method": "tools/call",
            "params": {
                "name": "edit_file",
                "arguments": {
                    "file": "main.py",
                    "expected_hash": digest(b"original\n"),
                    "content": "new\n",
                    "run_id": "another-run",
                },
            },
        },
    )
    assert result["isError"]
    assert (session.root / "main.py").read_text() == "original\n"


@pytest.mark.skipif(
    os.getenv("DEVORCHESTRA_TEST_SANDBOX") != "1", reason="opt-in actual CLI sandbox"
)
def test_installed_codex_readonly_boundary(setup):
    session, request = start_session(setup)
    CodexClient(Config().codex).check_workspace(request, session.path.parent, session, lambda: None)


def test_codex_stdio_transport_uses_host_edits_and_metadata_only(setup, tmp_path):
    session, request = start_session(setup)
    # A CLI stub drives the real stdio host server. No model or network is used.
    stub = tmp_path / "codex-stub"
    stub.write_text(f"""#!{sys.executable}
import json, subprocess, sys
from pathlib import Path
if 'sandbox' in sys.argv:
    raise SystemExit(0)
assert sys.argv[sys.argv.index('--sandbox') + 1] == 'read-only'
assert '--ignore-user-config' in sys.argv and '--ignore-rules' in sys.argv
assert '--ephemeral' not in sys.argv
schema = json.loads(Path(sys.argv[sys.argv.index('--output-schema') + 1]).read_text())
assert set(schema['properties']) == {{'summary', 'questions', 'no_change_reason', 'commit_message'}}
config = {{sys.argv[i+1].split('=', 1)[0]: sys.argv[i+1].split('=', 1)[1]
          for i, arg in enumerate(sys.argv[:-1]) if arg == '-c'}}
command = json.loads(config['mcp_servers.workspace.command'])
args = json.loads(config['mcp_servers.workspace.args'])
assert config['mcp_servers.workspace.required'] == 'true'
proc = subprocess.Popen([command, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
def call(method, params=None):
    proc.stdin.write(json.dumps({{'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {{}}}}) + '\\n')
    proc.stdin.flush()
    response = json.loads(proc.stdout.readline())
    assert 'error' not in response, response
    return response['result']
assert call('initialize')['protocolVersion'] == '2024-11-05'
assert len(call('tools/list')['tools']) == 3
result = call('tools/call', {{'name': 'edit_file', 'arguments': {{
    'file': 'ignored.py', 'expected_hash': None, 'content': 'actual host edit\\n'
}}}})
assert not result.get('isError'), result
file_hash = json.loads(result['content'][0]['text'])['hash']
result = call('tools/call', {{'name': 'checkpoint_commit', 'arguments': {{
    'request_id': 'transport', 'expected_sha': {request.input_sha!r},
    'files': {{'ignored.py': file_hash}}, 'message': '전송 계층 검증', 'summary': '실제 파일 편집'
}}}})
assert not result.get('isError'), result
proc.stdin.close()
assert proc.wait(timeout=10) == 0
assert 'workspace.edit_file' in sys.stdin.read()
Path(sys.argv[sys.argv.index('--output-last-message') + 1]).write_text(
    json.dumps({{'summary': '직접 편집 완료', 'questions': [], 'no_change_reason': '', 'commit_message': ''}}))
""")
    stub.chmod(0o755)
    from dev_orchestra.config import Codex

    client = CodexClient(Codex(command=str(stub)))
    response = client.run_workspace(request, session.path.parent, session)
    assert isinstance(response, WorkspaceResult)
    result, sha, commits = session.finish(response)
    assert session.git.call("show", sha + ":ignored.py") == "actual host edit"
    assert "ignored.py" in result.patch and len(commits) == 1


def test_workspace_readonly_probe_fails_closed(setup, monkeypatch):
    session, request = start_session(setup)
    import dev_orchestra.infrastructure.codex as adapter

    def fail(*args, **kwargs):
        return 1, "sandbox unavailable"

    monkeypatch.setattr(adapter, "execute", fail)
    with pytest.raises(PipelineError, match="boundary"):
        CodexClient(Config().codex).run_workspace(request, session.path.parent, session)
    assert session.git.sha() == request.input_sha


def test_partial_edits_invalid_response_preserves_work_and_does_not_advance(setup):
    svc, run_id, _ = setup
    # Advance through the actual planner gate before injecting an invalid final result.
    runner = Runner(svc.store, FakeAgent(), FakeTests())
    runner.execute(run_id)
    from dev_orchestra.application.plan_approval import binding, decide

    decide(svc, run_id, binding(svc.store, svc.store.get(run_id))["plan_hash"], True)

    class Invalid(FakeAgent):
        def run_workspace(self, req, directory, session, stop):
            edit(session, "main.py", "preserved\n")
            checkpoint(session, "saved", ["main.py"])
            edit(session, "pending.py", "pending\n")
            from dev_orchestra.agents.contracts import ResponseError

            raise ResponseError("invalid JSON summary")

    runner = Runner(svc.store, Invalid(), FakeTests())
    runner.execute(run_id)
    run = svc.store.get(run_id)
    assert run["state"] == "failed" and run["next_role"] == "developer"
    assert run["current_sha"] != run["base_sha"]
    assert (Path(run["worktree"]) / "pending.py").read_text() == "pending\n"
    assert not any(s["role"] == "tester" for s in svc.store.steps(run_id))


def test_checkpoint_with_unexpected_staged_files_preserves_index(setup):
    session, _ = start_session(setup)
    edit(session, "main.py", "edited\n")
    edit(session, "unrelated.py", "unrelated\n")
    session.git.call("add", "unrelated.py")
    with pytest.raises(PipelineError, match="staged"):
        checkpoint(session, "unit", ["main.py"])
    assert session.git.call("diff", "--cached", "--name-only") == "unrelated.py"
    assert session.git.sha() == session.data["input_sha"]


def test_no_change_needs_explicit_reason(setup):
    session, _ = start_session(setup)
    with pytest.raises(PipelineError, match="explicit reason"):
        session.finish(WorkspaceResult(summary="무변경"))
    result, sha, commits = session.finish(
        WorkspaceResult(summary="무변경", no_change_reason="기존 코드가 요구사항 충족")
    )
    assert not result.patch and not commits and sha == session.data["input_sha"]


def test_readonly_dependencies_and_untouched_symlinks_do_not_hide_new_source(setup):
    session, request = start_session(setup)
    # Typical host-prepared dependencies stay outside the editor's writable sources.
    (session.root / "node_modules/pkg").mkdir(parents=True)
    (session.root / "node_modules/pkg/index.js").write_text("dependency\n")
    with pytest.raises(PipelineError, match="output"):
        edit(session, "node_modules/pkg/index.js", "bad\n")
    edit(session, "new_source.py", "included\n")
    result, _, commits = session.finish(WorkspaceResult(summary="소스 추가"))
    assert "new_source.py" in result.patch and "node_modules" not in result.patch
    assert set(commits[0]["files"]) == {"new_source.py"}


def test_expired_role_cannot_use_editor(setup):
    session, _ = start_session(setup)
    setup[0].store.finish_step(session.data["step_id"], session.git.sha(), {})
    with pytest.raises(PipelineError, match="ownership expired"):
        edit(session, "main.py", "invalid\n")


def test_checkpoint_rejects_wrong_head_hash_and_empty_selection(setup):
    session, _ = start_session(setup)
    edit(session, "main.py", "new\n")
    for sha, files, match in [
        ("0" * 40, {"main.py": digest(b"new\n")}, "HEAD"),
        (session.git.sha(), {"main.py": "wrong"}, "checksum"),
        (session.git.sha(), {}, "selection"),
        (session.git.sha(), {"not-edited.py": digest(b"new\n")}, "selection"),
    ]:
        with pytest.raises(PipelineError, match=match):
            session.checkpoint_commit("invalid", sha, files, "잘못된 요청", "검증")
    assert not session.git.call("diff", "--cached", "--name-only")


@pytest.mark.skipif(
    os.getenv("DEVORCHESTRA_TEST_SANDBOX") != "1", reason="opt-in installed CLI config"
)
def test_installed_cli_accepts_workspace_mcp_configuration(setup):
    session, _ = start_session(setup)
    descriptor = session.path.parent / "launch.json"
    write(descriptor, {"session": str(session.path), "database": str(session.store.path)})
    argv = [
        "codex",
        "mcp",
        "list",
        "--json",
        "-c",
        "mcp_servers={}",
        "-c",
        "mcp_servers.workspace.command=" + json.dumps(sys.executable),
        "-c",
        "mcp_servers.workspace.args="
        + json.dumps(["-I", "-m", "dev_orchestra.infrastructure.workspace_mcp", str(descriptor)]),
        "-c",
        "mcp_servers.workspace.required=true",
        "-c",
        'mcp_servers.workspace.default_tools_approval_mode="approve"',
        "-c",
        'mcp_servers.workspace.enabled_tools=["edit_file","checkpoint_commit"]',
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, "installed Codex does not accept the host MCP configuration"
    servers = [item for item in json.loads(result.stdout) if item["name"] == "workspace"]
    assert len(servers) == 1 and servers[0]["enabled"]
