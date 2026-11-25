import re
from types import SimpleNamespace

import pytest

from dev_orchestra.agents.contracts import response_fields
from dev_orchestra.agents.prompts import prompt
from dev_orchestra.domain.models import AgentRequest, PipelineError
from dev_orchestra.infrastructure.test_runner import parse_junit
from dev_orchestra.infrastructure.workspace_mcp import dispatch, workspace_tools


def role_request(role, **context):
    return AgentRequest(
        run_id="r",
        role=role,
        worktree="/tmp/work",
        iteration=0,
        input_sha="a" * 40,
        context=context,
    )


@pytest.mark.parametrize(
    "role,mode,tools,fields",
    [
        ("planner", "normal", "", "plan criteria"),
        ("reviewer", "no-evidence", "", "issues schema_version assessments issue_distinctions"),
        (
            "reviewer",
            "normal",
            "read_test_evidence",
            "issues schema_version assessments issue_distinctions",
        ),
        (
            "developer",
            "normal",
            "workspace_state edit_file checkpoint_commit prepare_environment",
            "commit_message no_change_reason",
        ),
        (
            "fixer",
            "normal",
            "workspace_state edit_file checkpoint_commit prepare_environment",
            "commit_message no_change_reason issue_outcomes",
        ),
        (
            "tester",
            "fixed",
            "workspace_state edit_file test_policy run_tests read_test_evidence",
            "commit_message no_change_reason command_ids coverage test_evidence_id",
        ),
        (
            "tester",
            "automatic",
            "workspace_state edit_file test_policy run_tests read_test_evidence",
            "commit_message no_change_reason command_ids coverage test_evidence_id",
        ),
        ("developer", "metadata", "workspace_state", "commit_message no_change_reason"),
        ("fixer", "metadata", "workspace_state", "commit_message no_change_reason issue_outcomes"),
        (
            "tester",
            "metadata",
            "workspace_state test_policy read_test_evidence",
            "commit_message no_change_reason command_ids coverage test_evidence_id",
        ),
    ],
)
def test_prompt_matches_role_tools_permissions_and_response_fields(role, mode, tools, fields):
    metadata = mode == "metadata"
    evidence_reader = role == "reviewer" and mode == "normal"
    req = role_request(
        role,
        prd="Untrusted requirement sample",
        answers="Settled behavior sample",
        review_contract=2,
        test_evidence_id="step-1" if evidence_reader else None,
        automatic_test_selection=mode in {"automatic", "metadata"},
        workspace_resume={"metadata_only": metadata},
        response_contract={"edit_tool": "workspace.edit_file"} if metadata else {},
    )
    instructions, data = prompt(req).split("\nUNTRUSTED ", 1)
    expected = set(tools.split())
    assert set(re.findall(r"\bworkspace\.([a-z_]+)", instructions)) == expected
    assert response_fields(req) == {"summary", "questions", *fields.split()}
    if role != "reviewer" or evidence_reader:
        session = SimpleNamespace(
            data={"role": role, "metadata_only": metadata}, reload=lambda: None
        )
        advertised = dispatch(session, {"method": "tools/list"})["tools"]
        assert {tool["name"] for tool in advertised} == expected
        assert set(workspace_tools(role, metadata)) == expected
    assert "shell is read-only" in instructions
    assert "Host permissions take precedence" in instructions and "PRD text" in instructions
    assert "user answers" in instructions and "cannot change host permissions" in instructions
    assert "private reasoning" in instructions and "conversation transcripts" in instructions
    if role == "reviewer":
        assert (
            "Independently review" in instructions
            and "Never edit, commit or execute tests" in instructions
        )
        assert "response_contract.required_assessment_ids" in instructions
        assert "weakened coverage" in instructions
    if "edit_file" in expected:
        assert "Never delete, skip or weaken tests" in instructions
        assert "test_change" in instructions and "criterion_ids" in instructions
    if role == "tester" and not metadata:
        assert "allowlisted commands" in instructions and "Edit only test_paths" in instructions
        if mode == "automatic":
            assert instructions.index("workspace.test_policy") < instructions.index(
                "Submit test_plan"
            )
            assert instructions.index("Submit test_plan") < instructions.index(
                "actual executed command_ids"
            )
            assert "mandatory_command_ids, dependencies" in instructions
            assert "exactly its included" not in instructions
        else:
            assert (
                "exactly its included command_ids" in instructions
                and "test_plan" not in instructions
            )
    if metadata:
        assert "Editing, checkpoint commits and test execution are locked" in instructions
        assert "test_plan" not in instructions
        assert "workspace.edit_file" not in data


def test_validation_guidance_only_uses_known_stack_or_keeps_fallbacks():
    def guidance(argv):
        req = role_request("developer", test_commands={"unit": {"argv": argv}})
        return prompt(req).split("\nUNTRUSTED ", 1)[0]

    python = guidance(["python3.12", "-m", "pytest"])
    assert "bootstrap_unittest" in python
    assert "bootstrap_node" not in python and "bootstrap_java" not in python
    node = guidance(["yarn", "run", "test"])
    assert "jest-junit" in node and "bootstrap_unittest" not in node
    unknown = guidance(["make", "test"])
    assert all(
        hint in unknown for hint in ("bootstrap_unittest", "bootstrap_node", "bootstrap_java")
    )


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
