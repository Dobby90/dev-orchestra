import pytest

from dev_orchestra.agents.prompts import prompt
from dev_orchestra.domain.models import AgentRequest, PipelineError
from dev_orchestra.infrastructure.test_runner import parse_junit


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


@pytest.mark.parametrize("role", ["planner", "developer", "fixer", "tester", "reviewer"])
def test_wire_response_has_no_model_patch_fields(role):
    from dev_orchestra.agents.contracts import response_fields

    req = AgentRequest(
        run_id="r", role=role, worktree="/tmp/work", iteration=0, input_sha="abc", context={}
    )
    assert not {"patch", "test_changes", "test_plan"} & response_fields(req)
