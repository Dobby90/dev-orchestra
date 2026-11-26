"""모델 호출 없이 비교 과제 저장소를 준비하고 제출한 커밋을 독립 채점합니다."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.process import environment, execute
from dev_orchestra.infrastructure.test_runner import TestRunner, sandbox_command

ROOT = Path(__file__).resolve().parent
TASKS = {"label-list": ("labels", "parse_labels"), "usage-window": ("usage", "total_bytes")}
INITIAL_DATE = "2000-01-01T00:00:00+00:00"
# The child receives one input and the public entry point. Expected values stay with the grader.
INVOKE = """
import contextlib
import importlib
import json
import os
import sys

directory, module, name = sys.argv[1:]
os.chdir(directory)
sys.path.insert(0, directory)
with contextlib.redirect_stdout(sys.stderr):
    function = getattr(importlib.import_module(module), name)
    value = function(**json.load(sys.stdin))
print(json.dumps(value, ensure_ascii=False))
"""


def git(repo, *args):
    env = environment() | {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_DATE": INITIAL_DATE,
        "GIT_COMMITTER_DATE": INITIAL_DATE,
    }
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
        timeout=30,
    ).stdout.strip()


def prepare(task, dest):
    if dest.exists() or dest.is_symlink():
        raise ValueError(f"새 디렉터리 경로가 필요합니다: {dest}")
    module, _ = TASKS[task]
    source = ROOT / "tasks" / task
    with tempfile.TemporaryDirectory(dir=dest.parent) as temp:
        repo = Path(temp) / "repo"
        (repo / "tests").mkdir(parents=True)
        for name in ["PRD.md", f"{module}.py", f"tests/test_{module}.py"]:
            shutil.copyfile(source / name, repo / name)
        (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\nbuild/\n")
        git(repo, "init", "-q", "--template=", "--object-format=sha1", "-b", "main")
        git(repo, "config", "user.name", "Benchmark")
        git(repo, "config", "user.email", "benchmark@example.invalid")
        git(repo, "add", "--force", ".")
        git(repo, "commit", "-qm", "비교 과제 초기 상태")
        sha = git(repo, "rev-parse", "HEAD")
        repo.rename(dest)
    return {"task": task, "base_sha": sha, "repository": str(dest)}


def grade(task, repo, revision):
    if Path(git(repo, "rev-parse", "--show-toplevel")).resolve() != repo.resolve():
        raise ValueError("과제 Git 저장소의 루트를 지정해 주세요.")
    sha = git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")
    cases = json.loads((ROOT / "grading" / f"{task}.json").read_text())
    verdicts = {}
    with tempfile.TemporaryDirectory(prefix="devorchestra-grade-") as temp:
        root = Path(temp).resolve()
        snapshot = root / "candidate"
        snapshot.mkdir()
        git(
            snapshot,
            "init",
            "-q",
            "--template=",
            "--object-format=" + git(repo, "rev-parse", "--show-object-format"),
        )
        git(snapshot, "fetch", "-q", "--no-tags", "--depth=1", str(repo), sha)
        git(snapshot, "checkout", "-q", "--detach", "FETCH_HEAD")
        TestRunner().check(root)
        for case in cases:
            scratch = root / case["id"]
            scratch.mkdir()
            argv = sandbox_command(
                snapshot,
                [],
                scratch,
                [sys.executable, "-I", "-B", "-c", INVOKE, str(snapshot), *TASKS[task]],
            )
            try:
                status, output = execute(
                    argv,
                    root,
                    10,
                    root / (case["id"] + ".log"),
                    stdin=json.dumps(case["args"]),
                    capture_raw=True,
                    env=environment() | {"TMPDIR": str(scratch)},
                )
            except PipelineError as error:
                if str(error) not in {
                    "process timeout",
                    "process output exceeded 8 MiB safety limit",
                }:
                    raise
                verdicts[case["id"]] = False
                continue
            try:
                actual = json.loads(output)
            except ValueError:
                verdicts[case["id"]] = False
            else:
                expected = case["expected"]
                verdicts[case["id"]] = (
                    status == 0 and type(actual) is type(expected) and actual == expected
                )
    return {
        "task": task,
        "candidate_sha": sha,
        "passed": sum(verdicts.values()),
        "total": len(cases),
        "cases": verdicts,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [("prepare", "새 Git 저장소 생성"), ("grade", "커밋 SHA 독립 채점")]:
        command = commands.add_parser(name, help=help_text)
        command.add_argument("task", choices=TASKS)
        command.add_argument("repository", type=Path)
        if name == "grade":
            command.add_argument("--revision", default="HEAD")
    args = parser.parse_args()
    repo = args.repository.absolute()
    try:
        if args.command == "prepare":
            result = prepare(args.task, repo)
        else:
            result = grade(args.task, repo, args.revision)
    except (ValueError, OSError, subprocess.SubprocessError, PipelineError) as error:
        parser.exit(2, f"비교 과제 실행 오류: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(args.command == "grade" and result["passed"] != result["total"])


if __name__ == "__main__":
    sys.exit(main())
