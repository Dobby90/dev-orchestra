"""과제 커밋의 테스트를 동일한 기준 구현과 고정 회귀 결함에 실행합니다."""

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from compare import TASKS, git

from dev_orchestra.config import TestCommand, Tests
from dev_orchestra.infrastructure import unittest_bootstrap
from dev_orchestra.infrastructure.test_runner import TestRunner, verify_junit_artifact

LABELS = """def parse_labels(text, *, unique=False):
    labels = [part.strip() for part in text.split(",") if part.strip()]
    if not unique:
        return labels
    first = {}
    for label in labels:
        first.setdefault(label.casefold(), label)
    return list(first.values())
"""

WINDOW = """from datetime import datetime

def clock(value):
    return datetime.fromisoformat(value)

def total_bytes(events, start, end):
    lower, upper = clock(start), clock(end)
    total = 0
    for event in events:
        at = clock(event["at"])
        if lower <= at < upper:
            total += event["bytes"]
    return total
"""

MUTATIONS = {
    "label-list": (
        LABELS,
        {
            "unique-by-default": ("unique=False", "unique=True"),
            "ignore-unique": ("if not unique:", "if True:"),
            "lower-instead-of-casefold": ("label.casefold()", "label.lower()"),
            "last-spelling": (
                "first.setdefault(label.casefold(), label)",
                "first[label.casefold()] = label",
            ),
            "sorted-unique": ("list(first.values())", "sorted(first.values())"),
            "keep-empty-label": (" if part.strip()]", "]"),
            "keep-whitespace": ("[part.strip() for", "[part for"),
        },
    ),
    "usage-window": (
        WINDOW,
        {
            "include-end": ("at < upper", "at <= upper"),
            "exclude-start": ("lower <= at", "lower < at"),
            "ignore-offset": ("fromisoformat(value)", "fromisoformat(value).replace(tzinfo=None)"),
            "collapse-same-instant": (
                "for event in events:",
                'for event in {clock(e["at"]): e for e in events}.values():',
            ),
            "assume-sorted": (
                "if lower <= at",
                "if at >= upper:\n            break\n        if lower <= at",
            ),
            "float-result": ("return total", "return float(total)"),
            "nonzero-empty-window": (
                "total = 0",
                "if lower == upper:\n        return 1\n    total = 0",
            ),
        },
    ),
}


def variants(task):
    reference, changes = MUTATIONS[task]
    yield "reference", reference
    for name, (original, replacement) in changes.items():
        if reference.count(original) != 1:
            raise ValueError(f"결함 정의의 치환 위치가 유일하지 않습니다: {name}")
        yield name, reference.replace(original, replacement)


def suite_config(argv):
    return Tests(
        commands={
            "suite": TestCommand(
                argv=[sys.executable, "-B", *argv],
                junit_report="build/junit.xml",
                timeout_seconds=30,
            )
        }
    )


def score(task, repo, revision):
    sha = git(repo, "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}")
    config = suite_config(["-m", "pytest", "tests", "-q", "--junitxml=build/junit.xml"])
    killed, survived, rechecked = [], [], []
    with tempfile.TemporaryDirectory(prefix="devorchestra-mutations-") as temp:
        root = Path(temp).resolve()
        runner = TestRunner()
        runner.check(root)
        snapshot = root / "suite"
        snapshot.mkdir()
        git(
            snapshot,
            "init",
            "-q",
            "--template=",
            "--object-format=" + git(repo, "rev-parse", "--show-object-format"),
        )
        git(snapshot, "fetch", "-q", "--no-tags", "--depth=1", str(repo), sha)
        entries = git(snapshot, "ls-tree", "-r", "FETCH_HEAD", "--", "tests").splitlines()
        if not entries or any(row.split(" ", 1)[0] not in {"100644", "100755"} for row in entries):
            raise ValueError("tests 아래의 일반 파일이 필요합니다")
        git(snapshot, "checkout", "-q", "FETCH_HEAD", "--", "tests")
        for name, source in variants(task):
            work = root / name
            work.mkdir()
            git(work, "init", "-q", "--template=")
            shutil.copytree(snapshot / "tests", work / "tests")
            (work / f"{TASKS[task][0]}.py").write_text(source)
            execution = runner.run(work, config, root / (name + "-results"))
            command = execution["commands"][0]
            if (
                name != "reference"
                and command["status"] == "parse_error"
                and command["exit_code"] == 1
                and "suite failure counts disagree" in command.get("error", "")
            ):
                # pytest may count several failed unittest subtests under one testcase.
                # Require a fresh JUnit report with the full original test inventory below.
                native = suite_config([str(Path(unittest_bootstrap.__file__).resolve())])
                command = runner.run(work, native, root / (name + "-unittest"))["commands"][0]
                if command["passed"]:
                    raise ValueError(f"{name}: 두 실행기가 실패를 재현하지 못했습니다")
                rechecked.append(name)
            if not command.get("junit_path"):
                raise ValueError(f"{name}: 실행 근거 누락 ({command['status']})")
            tree = verify_junit_artifact(Path(command["junit_path"]), command)
            identities = sorted(
                ".".join(filter(None, [case.get("classname"), case.attrib["name"]])).removeprefix(
                    "tests."
                )
                for case in tree.iter("testcase")
            )
            if name == "reference":
                if not command["passed"] or command["skipped"]:
                    raise ValueError("기준 구현에서 테스트가 실패하거나 생략되었습니다")
                reference_tests = identities
                continue
            if identities != reference_tests or command["skipped"]:
                raise ValueError(f"{name}: 실행한 테스트 목록 또는 상태가 대조군과 다릅니다")
            if command["passed"]:
                survived.append(name)
                continue
            failures = tree.findall(".//testcase/failure")
            if (
                command["exit_code"] != 1
                or not failures
                or tree.find(".//testcase/error") is not None
                or not all(
                    item.get("message", "").startswith(("AssertionError", "assert "))
                    or re.search(r"^AssertionError(?::|$)", item.text or "", re.MULTILINE)
                    for item in failures
                )
            ):
                raise ValueError(f"{name}: 단언 실패로 판정할 수 없습니다")
            killed.append(name)
    return {
        "task": task,
        "sha": sha,
        "tests": len(reference_tests),
        "killed": killed,
        "survived": survived,
        "unittest_rechecked": rechecked,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", choices=TASKS)
    parser.add_argument("repository", type=Path)
    parser.add_argument("--revision", default="HEAD")
    args = parser.parse_args()
    print(
        json.dumps(
            score(args.task, args.repository.resolve(), args.revision), ensure_ascii=False, indent=2
        )
    )
