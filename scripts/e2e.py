"""Explicitly opt-in real Codex smoke test; never called by normal pytest."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from dev_orchestra.application.service import RunService
from dev_orchestra.config import Config
from dev_orchestra.infrastructure.codex import CodexClient
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.test_runner import TestRunner
from dev_orchestra.orchestration.runner import Runner


def main():
    root = Path(tempfile.mkdtemp(prefix="devorchestra-e2e-"))
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git = Git(repo)
    git.call("config", "user.name", "DevOrchestra E2E")
    git.call("config", "user.email", "e2e@example.invalid")
    (repo / "calculator.py").write_text('"""Small calculator module."""\n')
    git.commit("fixture")
    spec = root / "PRD.md"
    spec.write_text(
        "Add add(a: int, b: int) -> int in calculator.py returning a+b. Support negative and zero values. Add pytest tests under tests/. Do not add dependencies. Keep changes minimal."
    )
    config = Config.model_validate(
        {
            "storage": {
                "database": str(root / "state.db"),
                "runs_directory": str(root / "runs"),
                "worktrees_directory": str(root / "worktrees"),
            },
            "codex": {"timeout_seconds": 240},
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
    client = CodexClient(config.codex)
    client.check()
    TestRunner().check(repo)
    service = RunService(config)
    run_id = service.create(repo, spec)
    print(json.dumps({"directory": str(root), "run_id": run_id}), flush=True)
    Runner(service.store, client, TestRunner()).execute(run_id)
    result = service.store.get(run_id)
    if result["state"] == "waiting_plan_approval":
        from dev_orchestra.application.plan_approval import binding, decide

        decide(service, run_id, binding(service.store, result)["plan_hash"], True)
        Runner(service.store, client, TestRunner()).execute(run_id)
        result = service.store.get(run_id)
    print(
        json.dumps(
            {
                "state": result["state"],
                "error": result.get("error"),
                "questions": result.get("questions"),
            }
        ),
        flush=True,
    )
    if result["state"] != "waiting_approval":
        return 1
    service.approve(run_id)
    assert git.dirty() == ""
    print("REAL E2E PASSED: verified, approved, original checkout unchanged", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
