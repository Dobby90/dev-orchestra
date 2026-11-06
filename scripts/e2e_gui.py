"""Opt-in paid Codex E2E: actual HTTP/Dispatcher/Worker and a documented injected fault."""

import argparse
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from dev_orchestra.api.security import bootstrap
from dev_orchestra.application.results import Results
from dev_orchestra.application.service import RunService
from dev_orchestra.application.web_runtime import WebRuntime
from dev_orchestra.config import Config
from dev_orchestra.infrastructure.files import write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.migrations import upgrade


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-model-calls", action="store_true")
    parser.add_argument("--model", required=True)
    args = parser.parse_args()
    if not args.allow_model_calls:
        parser.error("실제 모델 사용 승인이 필요합니다: --allow-model-calls")
    codex = shutil.which("codex")
    if not codex:
        parser.error("Codex CLI가 없습니다")
    root = Path(tempfile.mkdtemp(prefix="devorchestra-gui-real-")).resolve()
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git = Git(repo)
    git.call("config", "user.name", "GUI E2E")
    git.call("config", "user.email", "e2e@example.invalid")
    (repo / "calculator.py").write_text(
        "def add(a: int, b: int) -> int:\n    raise NotImplementedError\n"
    )
    (repo / "tests").mkdir()
    (repo / "tests/test_existing.py").write_text(
        "from calculator import add\ndef test_existing():\n    assert add(2, 3) == 5\n    assert add(-2, 0) == -2\n"
    )
    (repo / ".gitignore").write_text("build/\n.pytest_cache/\n__pycache__/\n")
    git.commit("실제 GUI 검증 기준선")
    baseline = git.sha()
    (repo / "local-note.txt").write_text("보존할 사용자 로컬 작업\n")
    original_status = git.dirty()
    proxy = root / "codex-fixture"
    proxy.write_text(f"""#!{sys.executable}
import json,re,subprocess,sys
from pathlib import Path
real={codex!r}
ledger=Path({str(root / "calls.json")!r})
if '--help' in sys.argv or 'sandbox' in sys.argv:raise SystemExit(subprocess.call([real,*sys.argv[1:]]))
data=sys.stdin.read()
marker='UNTRUSTED TASK DATA:\\n' if 'UNTRUSTED TASK DATA:\\n' in data else 'UNTRUSTED CONTINUATION DATA:\\n'
req=json.loads(data.split(marker,1)[1])
calls=json.loads(ledger.read_text()) if ledger.exists() else []
if len(calls)>=7:raise SystemExit('model call limit reached')
calls.append(req['role']);ledger.write_text(json.dumps(calls));ledger.chmod(0o600)
rc=subprocess.run([real,*sys.argv[1:]],input=data,text=True).returncode
if rc:raise SystemExit(rc)
if req['role']=='developer' and calls.count('developer')==1:
 path=Path(sys.argv[sys.argv.index('--output-last-message')+1])
 raw=path.read_text();value=json.loads(raw)
 config={{sys.argv[i+1].split('=',1)[0]:sys.argv[i+1].split('=',1)[1] for i,arg in enumerate(sys.argv[:-1]) if arg=='-c'}}
 from dev_orchestra.infrastructure.workspace import load_session
 from dev_orchestra.infrastructure.files import digest
 session=load_session(json.loads(config['mcp_servers.workspace.args'])[-1])
 source=session.root/'calculator.py'
 content,count=re.subn(r'(?m)^(\\s+return a)\\s*\\+\\s*(b\\s*)$',r'\\1 - \\2',source.read_text())
 if count!=1:raise SystemExit('fault injection shape mismatch; no retry')
 original=Path({str(root / "original-developer.json")!r});original.write_text(raw);original.chmod(0o600)
 session.edit_file('calculator.py',digest(source.read_bytes()),content)
 value['commit_message']='검증용 산술 결함 주입\\n\\n자동 수정 루프가 실제 테스트 실패를 처리하는지 확인합니다.'
 path.write_text(json.dumps(value))
""")
    proxy.chmod(0o700)
    config = Config.model_validate(
        {
            "storage": {
                "database": str(root / "state.db"),
                "runs_directory": str(root / "runs"),
                "worktrees_directory": str(root / "worktrees"),
            },
            "codex": {"command": str(proxy), "model": args.model, "timeout_seconds": 180},
            "workflow": {"max_fix_attempts": 1},
            "tests": {
                "commands": {
                    "unit": {
                        "argv": [
                            sys.executable,
                            "-m",
                            "pytest",
                            "tests",
                            "--junitxml=build/junit.xml",
                            "-q",
                        ],
                        "junit_report": "build/junit.xml",
                    }
                }
            },
        }
    )
    config_path = root / "config.yaml"
    write(config_path, config.model_dump(mode="json"))
    service = RunService(config)
    upgrade(service.store.path, workspace=True)
    runtime = WebRuntime(service)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    print(
        json.dumps(
            {
                "root": str(root),
                "model": args.model,
                "max_calls": 7,
                "fault_injection": "developer addition changed to subtraction by fixture",
            }
        ),
        flush=True,
    )
    rid = None
    try:
        runtime.start([root], port, True)
        url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=url, headers={"Origin": url}, timeout=20) as client:
            response = client.post(
                "/api/session", json={"bootstrap_code": bootstrap(service.store)}
            )
            assert response.status_code == 201, response.text
            client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
            response = client.post(
                "/api/projects",
                json={
                    "name": "실제 Codex 검증",
                    "repository_path": str(repo),
                    "policy": {
                        "tests": config.tests.model_dump(mode="json"),
                        "max_fix_attempts": 1,
                        "blocking_severities": ["critical", "high"],
                    },
                },
            )
            assert response.status_code == 201, response.text
            pid = response.json()["id"]
            proposal = {
                "name": "덧셈 기능 검증",
                "project_id": pid,
                "spec": {
                    "kind": "text",
                    "text": "Implement add(a: int, b: int) -> int in calculator.py returning exactly a + b. Support positive, negative and zero integers. Keep existing tests immutable and add a new pytest file under tests/ for additional cases. Do not add dependencies or extra type validation. Developer: use exactly return a + b as the implementation. Keep changes minimal.",
                },
            }
            response = client.post(f"/api/projects/{pid}/run-previews", json=proposal)
            assert response.status_code == 200, response.text
            response = client.post(
                "/api/runs",
                json=proposal | {"preview_token": response.json()["preview_token"]},
                headers={"Idempotency-Key": "real-e2e-one"},
            )
            assert response.status_code == 202, response.text
            rid = response.json()["run_id"]
            print(json.dumps({"run_id": rid}), flush=True)
            previous = None
            deadline = time.monotonic() + 1500
            while time.monotonic() < deadline:
                value = client.get(f"/api/runs/{rid}").json()
                signature = (value["state"], value["iteration"])
                if signature != previous:
                    print(
                        json.dumps({"state": value["state"], "iteration": value["iteration"]}),
                        flush=True,
                    )
                    previous = signature
                if value["state"] == "waiting_plan_approval":
                    response = client.post(
                        f"/api/runs/{rid}/approve-plan",
                        json={"plan_hash": value["plan_hash"]},
                        headers={"If-Match": f'"run:{rid}:v{value["version"]}"'},
                    )
                    assert response.status_code in {202, 409}, response.text
                if value["state"] in {"waiting_approval", "needs_input", "failed", "cancelled"}:
                    break
                time.sleep(1)
            run = service.store.get(rid)
            write(
                root / "outcome.json",
                {
                    "state": run["state"],
                    "error": run.get("error"),
                    "questions": run.get("questions"),
                    "calls": json.loads((root / "calls.json").read_text()),
                },
            )
            assert run["state"] == "waiting_approval", (
                run["state"],
                run.get("error"),
                run.get("questions"),
            )
            steps = service.store.steps(rid)
            tests = [s for s in steps if s["role"] == "tester"]
            assert len(tests) == 2
            assert Results(service.store).tests(rid, tests[0]["id"])["status"] == "failed"
            assert Results(service.store).tests(rid)["status"] == "passed"
            review = client.get(f"/api/runs/{rid}/reviews").json()
            blocking = [i for i in review["issues"] if i["severity"] in {"critical", "high"}]
            assert blocking and all(i["status"] == "resolved" for i in blocking)
            assert any(s["role"] == "fixer" for s in steps)
            cli = Path(sys.executable).parent / "devorchestra"
            subprocess.run(
                [str(cli), "--config", str(config_path), "approve", rid],
                check=True,
                capture_output=True,
                text=True,
            )
            assert client.get(f"/api/runs/{rid}").json()["state"] == "completed"
            assert git.sha() == baseline and git.dirty() == original_status
            assert (repo / "local-note.txt").read_text() == "보존할 사용자 로컬 작업\n"
            write(
                root / "outcome.json",
                {
                    "state": "completed",
                    "calls": json.loads((root / "calls.json").read_text()),
                    "test_history": [
                        Results(service.store).tests(rid, s["id"])["status"] for s in tests
                    ],
                    "resolved_issues": len(
                        [i for i in review["issues"] if i["status"] == "resolved"]
                    ),
                    "remaining_nonblocking_issues": [
                        i for i in review["issues"] if i["status"] != "resolved"
                    ],
                    "original_preserved": True,
                },
            )
            print(
                "REAL GUI E2E PASSED: HTTP → Worker → injected failure → actual Fix/Test/Review V2 → CLI approval",
                flush=True,
            )
    finally:
        if rid and service.store.get(rid)["state"] not in {
            "completed",
            "cancelled",
            "waiting_approval",
        }:
            service.cancel(rid)
        runtime.stop()
        deadline = time.monotonic() + 15
        while runtime.status()["alive"] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not runtime.status()["alive"]


if __name__ == "__main__":
    main()
