import subprocess

import pytest

from dev_orchestra.agents.policy import apply_patch
from dev_orchestra.agents.prompts import prompt
from dev_orchestra.domain.models import AgentRequest, PipelineError
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.test_runner import parse_junit


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    git = Git(tmp_path)
    git.call("config", "user.name", "Test")
    git.call("config", "user.email", "test@example.invalid")
    (tmp_path / "main.py").write_text("old\n")
    git.commit("initial")
    return tmp_path


def patch(name):
    return f"diff --git a/{name} b/{name}\n--- a/{name}\n+++ b/{name}\n@@ -1 +1 @@\n-old\n+new\n"


def test_test_agent_cannot_change_production(repo):
    with pytest.raises(PipelineError, match="production"):
        apply_patch(repo, patch("main.py"), "tester", ["tests"])
    assert (repo / "main.py").read_text() == "old\n"


def test_developer_patch(repo):
    apply_patch(repo, patch("main.py"), "developer", ["tests"])
    assert (repo / "main.py").read_text() == "new\n"


def test_symlink_patch(repo):
    (repo / "tests").symlink_to(repo.parent, target_is_directory=True)
    with pytest.raises(PipelineError):
        apply_patch(repo, patch("tests/main.py"), "tester", ["tests"])


def test_review_prompt_is_independent():
    req = AgentRequest(
        run_id="r",
        role="reviewer",
        worktree="/tmp/work",
        iteration=0,
        input_sha="abc",
        context={"prd": "test"},
    )
    assert "Independently" in prompt(req)
    assert "private reasoning" in prompt(req)


def test_junit_requires_executed_tests(tmp_path):
    path = tmp_path / "results.xml"
    path.write_text('<testsuite tests="0"/>')
    with pytest.raises(PipelineError):
        parse_junit(path)
    path.write_text(
        '<testsuite><testcase name="works"/><testcase name="bad"><failure>oops</failure></testcase></testsuite>'
    )
    assert parse_junit(path)["failed"] == 1


def test_miscounted_patch_is_recounted(repo):
    source = patch("main.py").replace("@@ -1 +1 @@", "@@ -1,4 +1,9 @@")
    apply_patch(repo, source, "developer", ["tests"])
    assert (repo / "main.py").read_text() == "new\n"


def test_existing_tests_are_protected(repo):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_main.py").write_text("old\n")
    with pytest.raises(PipelineError, match="protected"):
        apply_patch(repo, patch("tests/test_main.py"), "fixer", ["tests"])
