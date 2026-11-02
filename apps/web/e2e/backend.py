"""Disposable native API/Dispatcher fixture. Never uses a user's DB or repository."""

import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
import yaml

from dev_orchestra.api.main import create_app
from dev_orchestra.application.service import RunService
from dev_orchestra.config import Config, Storage, TestCommand, Tests
from dev_orchestra.infrastructure.migrations import upgrade

root = Path(tempfile.mkdtemp(prefix="devorchestra-web-e2e-")).resolve()
repo = root / "repository"
repo.mkdir()


def git(*args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


git("init", "-b", "main")
git("config", "user.name", "GUI 검사")
git("config", "user.email", "gui-test@localhost")
(repo / "main.py").write_text("def add(a, b):\n    return a + b\n")
(repo / "tests").mkdir()
(repo / "tests/test_main.py").write_text(
    "from main import add\ndef test_add():\n    assert add(2, 3) == 5\n"
)
git("add", ".")
git("commit", "-m", "임시 GUI 검증 기준선")
release = root / "release"
mock = root / "mock-codex"
mock.write_text(f"""#!{sys.executable}
import json,sys,time
from pathlib import Path
from dev_orchestra.domain.models import AgentResult, Criterion, Coverage
if '--help' in sys.argv:
 print('--output-schema --ephemeral --ignore-user-config --json');sys.exit(0)
req=json.loads(sys.stdin.read().split('UNTRUSTED TASK DATA:\\n',1)[1])
if req['role']=='developer':
 print('GUI 실시간 한글 로그 token=CANARY',flush=True)
 while not Path({str(release)!r}).exists():time.sleep(.05)
r=AgentResult(summary='GUI 검증')
if req['role']=='planner':r.plan='덧셈 검증';r.criteria=[Criterion(id='AC1',description='덧셈 통과')]
if req['role']=='tester':r.command_ids=['unit'];r.coverage=[Coverage(criterion_id='AC1',command_ids=['unit'])]
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(r.model_dump_json())
""")
mock.chmod(0o700)
config = Config(
    storage=Storage(
        database=root / "state.db",
        runs_directory=root / "runs",
        worktrees_directory=root / "worktrees",
    ),
    tests=Tests(
        commands={
            "unit": TestCommand(
                argv=[sys.executable, "-m", "pytest", "tests", "--junitxml=build/junit.xml", "-q"],
                junit_report="build/junit.xml",
            )
        },
        output_paths=["build", ".pytest_cache"],
    ),
)
config.codex.command = str(mock)
settings = root / "config.yaml"
settings.write_text(yaml.safe_dump(config.model_dump(mode="json")))
svc = RunService(config)
upgrade(svc.store.path)
manifest = Path("test-results/fixture.json")
manifest.parent.mkdir(parents=True, exist_ok=True)
manifest.write_text(
    json.dumps(
        {
            "root": str(root),
            "repo": str(repo),
            "database": str(svc.store.path),
            "release": str(release),
            "config": str(settings),
            "python": sys.executable,
            "policy": {
                "tests": config.tests.model_dump(mode="json"),
                "max_fix_attempts": 3,
                "blocking_severities": ["critical", "high"],
            },
            "base_sha": git("rev-parse", "HEAD").stdout.strip(),
        }
    )
)
os.chmod(manifest, 0o600)


@asynccontextmanager
async def lifespan(app):
    with (root / "dispatcher.log").open("w") as log:
        dispatcher = subprocess.Popen(
            [sys.executable, "-m", "dev_orchestra.execution.dispatcher", str(svc.store.path)],
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            yield
        finally:
            for run in svc.store.list():
                if run["state"] not in {"cancelled", "completed"}:
                    svc.cancel(run["id"])
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with svc.store.connection() as db:
                    if not db.execute(
                        "SELECT 1 FROM jobs WHERE state IN ('claimed','running')"
                    ).fetchone():
                        break
                time.sleep(0.1)
            dispatcher.terminate()
            try:
                dispatcher.wait(timeout=10)
            except subprocess.TimeoutExpired:
                dispatcher.kill()
                dispatcher.wait()


app = create_app(config, [root], port=18080)
app.router.lifespan_context = lifespan
uvicorn.run(app, host="127.0.0.1", port=18080, proxy_headers=False, log_level="warning")
