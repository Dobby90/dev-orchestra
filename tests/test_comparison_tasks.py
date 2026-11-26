"""Offline task calibration using standalone repositories and an external answer key."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

DRIVER = Path(__file__).resolve().parents[1] / "scripts" / "benchmarks" / "compare.py"
TASKS = [
    ("label-list", "labels", {"legacy-order"}),
    ("usage-window", "usage", {"ordinary", "start-inclusive-duplicates"}),
]


def compare(*args, expected=0):
    completed = subprocess.run(
        [sys.executable, DRIVER, *args], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == expected, (completed.stdout, completed.stderr)
    return json.loads(completed.stdout)


@pytest.mark.parametrize("task,module,_", TASKS)
def test_prepared_task_is_reproducible_and_contains_only_public_files(tmp_path, task, module, _):
    repo = tmp_path / "first"
    first = compare("prepare", task, repo)
    second = compare("prepare", task, tmp_path / "second")
    assert first["base_sha"] == second["base_sha"]
    files = subprocess.check_output(["git", "ls-files"], cwd=repo, text=True).splitlines()
    assert files == sorted([".gitignore", "PRD.md", f"{module}.py", f"tests/test_{module}.py"])
    subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"], cwd=repo, check=True
    )
    subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--junitxml=build/junit.xml"],
        cwd=repo,
        check=True,
    )
    assert subprocess.check_output(["git", "status", "--porcelain"], cwd=repo) == b""


@pytest.mark.skipif(
    os.getenv("DEVORCHESTRA_TEST_SANDBOX") != "1", reason="실제 OS 샌드박스 선택 검사"
)
@pytest.mark.parametrize("task,module,baseline_passes", TASKS)
def test_independent_cases_score_broken_and_corrected_commits(
    tmp_path, task, module, baseline_passes
):
    repo = tmp_path / task
    base = compare("prepare", task, repo)["base_sha"]
    original = compare("grade", task, repo, expected=1)
    assert original["candidate_sha"] == base
    assert {case for case, passed in original["cases"].items() if passed} == baseline_passes
    source = repo / f"{module}.py"
    if task == "label-list":
        source.write_text(
            "def parse_labels(text, *, unique=False):\n"
            "    labels = [p.strip() for p in text.split(',') if p.strip()]\n"
            "    if not unique:\n"
            "        return labels\n"
            "    first = {}\n"
            "    for label in labels:\n"
            "        first.setdefault(label.casefold(), label)\n"
            "    return list(first.values())\n"
        )
    else:
        source.write_text(
            source.read_text().replace(".replace(tzinfo=None)", "").replace("<= upper", "< upper")
        )
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qam",
            "독립 채점 대조군 수정",
        ],
        cwd=repo,
        check=True,
    )
    # The grader must use the submitted commit even with pending edits.
    source.write_text("raise RuntimeError('커밋하지 않은 변경')\n")
    fixed = compare("grade", task, repo)
    assert fixed["candidate_sha"] != base
    assert fixed["passed"] == fixed["total"] == len(original["cases"])
    assert all(fixed["cases"].values())
    assert compare("grade", task, repo, "--revision", base, expected=1) == original


@pytest.mark.skipif(
    os.getenv("DEVORCHESTRA_TEST_SANDBOX") != "1", reason="실제 OS 샌드박스 선택 검사"
)
@pytest.mark.parametrize("task,module,_", TASKS)
def test_reference_and_each_mutant_have_an_independent_witness(tmp_path, task, module, _):
    repo = tmp_path / task
    compare("prepare", task, repo)
    cases = json.loads((DRIVER.parent / "grading" / f"{task}.json").read_text())
    if task == "usage-window":
        # A later event preceding an in-range one exposes early exit on unsorted input.
        case = next(case for case in cases if case["id"] == "mixed-bound-offsets")
        args = case["args"] | {"events": list(reversed(case["args"]["events"]))}
        cases.append({"args": args, "expected": case["expected"]})
    function = "parse_labels" if task == "label-list" else "total_bytes"
    (repo / "tests" / "test_oracle.py").write_text(
        "import unittest\n"
        f"from {module} import {function}\n"
        f"CASES = {cases!r}\n"
        "class Oracle(unittest.TestCase):\n"
        "    def test_independent_examples(self):\n"
        "        for case in CASES:\n"
        "            with self.subTest(case=case):\n"
        f"                actual = {function}(**case['args'])\n"
        "                self.assertIs(type(actual), type(case['expected']))\n"
        "                self.assertEqual(actual, case['expected'])\n"
    )
    subprocess.run(["git", "add", "tests/test_oracle.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "고정 결함 대조 검사",
        ],
        cwd=repo,
        check=True,
    )
    result = subprocess.run(
        [sys.executable, DRIVER.with_name("mutation_check.py"), task, repo],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    verdict = json.loads(result.stdout)
    assert verdict["tests"] == 3
    assert len(verdict["killed"]) == 7
    assert verdict["survived"] == []
