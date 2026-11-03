"""Launcher contracts; native services use only a disposable state database."""

import json
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from dev_orchestra.application.service import RunService
from dev_orchestra.application.web_runtime import WebRuntime
from dev_orchestra.config import Config
from dev_orchestra.infrastructure.locking import lock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "start.sh"


@pytest.mark.parametrize("args", [["--port", "bad"], ["--port", "0"], ["--port"], ["--unknown"]])
def test_invalid_options_stop_before_launch(args, tmp_path):
    result = subprocess.run([str(SCRIPT), *args], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 1 and "오류:" in result.stderr
    assert not list(tmp_path.iterdir())


def test_existing_default_service_needs_no_install_or_database_upgrade(tmp_path):
    root = tmp_path / "app with spaces"
    root.mkdir()
    script = root / "start.sh"
    script.write_bytes(SCRIPT.read_bytes())
    script.chmod(0o755)
    bin_dir = root / ".venv/bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python"
    python.write_text("#!/bin/bash\nexec " + shlex.quote(sys.executable) + ' "$@"\n')
    python.chmod(0o755)
    log = root / "calls.jsonl"
    cli = bin_dir / "devorchestra"
    cli.write_text(f"""#!{sys.executable}
import json,sys
from pathlib import Path
with Path({str(log)!r}).open('a') as log:log.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1:]==['web','status']:print(json.dumps({{'alive':True,'port':9876}}))
elif sys.argv[1:]==['web-session']:print('FIXTURE_LOGIN_CODE')
else:raise SystemExit('unexpected mutation')
""")
    cli.chmod(0o755)
    result = subprocess.run([str(script)], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "http://127.0.0.1:9876" in result.stdout and "FIXTURE_LOGIN_CODE" in result.stdout
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        ["web", "status"],
        ["web-session"],
    ]
    assert not (root / "src").exists() and not (root / "node_modules").exists()


def test_native_start_relative_config_repeat_and_rebuild_guard(tmp_path):
    config = Config.model_validate(
        {
            "storage": {
                "database": str(tmp_path / "state/state.db"),
                "runs_directory": str(tmp_path / "state/runs"),
                "worktrees_directory": str(tmp_path / "state/worktrees"),
            }
        }
    )
    config_file = tmp_path / "settings with spaces.yaml"
    config_file.write_text(config.model_dump_json())
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    args = [str(SCRIPT), "--config", config_file.name, "--allow-root", ".", "--port", str(port)]
    runtime = None
    try:
        first = subprocess.run(args, cwd=tmp_path, capture_output=True, text=True, timeout=45)
        svc = RunService(config)
        runtime = WebRuntime(svc)
        assert first.returncode == 0, first.stderr
        assert f"http://127.0.0.1:{port}" in first.stdout
        owner = runtime.status()
        assert owner["alive"] and httpx.get(f"http://127.0.0.1:{port}/").status_code == 200
        with svc.store.connection() as db:
            assert db.execute("SELECT version FROM schema_version").fetchone()[0] == 5
        # A repeated launcher must reuse the existing port and process.
        second = subprocess.run(
            args[:-1] + ["8001"], cwd=tmp_path, capture_output=True, text=True, timeout=15
        )
        assert second.returncode == 0 and "이미 실행 중" in second.stdout
        assert f"http://127.0.0.1:{port}" in second.stdout
        assert runtime.status()["pid"] == owner["pid"]
        with svc.store.connection() as db:
            assert db.execute("SELECT count(*) FROM web_bootstrap_codes").fetchone()[0] == 2
            assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        manifest = ROOT / "src/dev_orchestra/_web/manifest.json"
        before = manifest.read_bytes()
        refused = subprocess.run(
            args + ["--rebuild"], cwd=tmp_path, capture_output=True, text=True, timeout=15
        )
        assert refused.returncode == 1 and "종료한 뒤" in refused.stderr
        assert manifest.read_bytes() == before and runtime.status()["pid"] == owner["pid"]
        runtime.stop()
        deadline = time.monotonic() + 15
        while runtime.status()["alive"] and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not runtime.status()["alive"]
        # A current-schema DB may be used while a Run Worker holds this shared gate.
        with lock(svc.store.path.with_suffix(".maintenance.lock"), shared=True):
            restarted = subprocess.run(
                args, cwd=tmp_path, capture_output=True, text=True, timeout=30
            )
            assert restarted.returncode == 0, restarted.stderr
            assert runtime.status()["alive"]
    finally:
        if runtime:
            runtime.stop()
            deadline = time.monotonic() + 15
            while runtime.status()["alive"] and time.monotonic() < deadline:
                time.sleep(0.1)
            assert not runtime.status()["alive"]
