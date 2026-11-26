import re
from types import SimpleNamespace

import pytest

from dev_orchestra.agents.contracts import response_fields
from dev_orchestra.agents.prompts import continuation_prompt, prompt
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


@pytest.mark.parametrize("role", ["developer", "fixer"])
def test_workspace_prompts_request_summary_and_path_hashes(role):
    req = role_request(role)
    for render in (prompt, continuation_prompt):
        instructions = render(req).split("\nUNTRUSTED ", 1)[0]
        assert "workspace.workspace_state(summary_only=true)" in instructions
        assert "batch only the needed repo-relative paths" in instructions
        assert "workspace.workspace_state(paths=[...])" in instructions
        metadata = render(role_request(role, workspace_resume={"metadata_only": True})).split(
            "\nUNTRUSTED ", 1
        )[0]
        assert "workspace.workspace_state(summary_only=true)" in metadata
        assert "workspace.workspace_state(paths=[...])" not in metadata


def test_developer_and_tester_prompts_agree_on_handoff():
    for render in (prompt, continuation_prompt):
        assert "Use summary to hand off to Tester" in render(role_request("developer"))
        request = role_request(
            "tester",
            development_handoff={
                "developer_summary": "Verify stale selections",
                "changed_files": ["log.vue"],
            },
        )
        instructions, data = render(request).split("\nUNTRUSTED ", 1)
        assert "Check them against the actual candidate diff" in instructions
        assert "Only actual host test results establish verification" in instructions
        assert "Verify stale selections" in data and "log.vue" in data
        request.context["workspace_resume"] = {"metadata_only": True}
        assert "development_handoff" not in render(request)


@pytest.mark.parametrize("metadata_only", [False, True])
def test_tester_uses_summary_before_fetching_evidence(metadata_only):
    request = role_request("tester", workspace_resume={"metadata_only": metadata_only})
    instructions = prompt(request).split("\nUNTRUSTED ", 1)[0]
    assert "Use the summary alone for passed command status" in instructions
    assert "when you need exact JUnit names or qualifiers" in instructions
    assert "for a command omitted from the summary or for a failure" in instructions
    assert "default page size and continue by offset only as needed" in instructions
    tester = workspace_tools("tester", metadata_only)
    assert "For passed commands use the execution summary" in tester["read_test_evidence"][1]
    assert "Omit command_id" in workspace_tools("reviewer")["read_test_evidence"][1]


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


@pytest.mark.parametrize("brief_plan", [False, True], ids=["detailed", "brief"])
def test_planner_delegates_registered_external_runner_to_host(brief_plan):
    request = role_request(
        "planner",
        brief_plan=brief_plan,
        test_commands={
            "partition-date": {
                "argv": ["/opt/validation/check"],
                "junit_report": "build/bench/junit.xml",
            }
        },
    )
    instructions, data = prompt(request).split("\nUNTRUSTED TASK DATA:\n", 1)
    assert "Tester requests registered commands by ID" in instructions
    assert "host runs them in the test sandbox" in instructions
    assert "outside the worktree" in instructions and "continue planning" in instructions
    assert "partition-date" in data and "/opt/validation/check" in data


def test_brief_planner_retains_full_criteria_and_falls_back_for_dashboard_races():
    task = {
        "prd": "대상 A → B → A에서 이전 응답과 잘못된 선택 이벤트를 무시한다.",
        "test_commands": {"unit": {"argv": ["yarn", "run", "test"]}},
    }
    default = prompt(role_request("planner", **task))
    request = role_request("planner", brief_plan=True, **task)
    brief = prompt(request)
    assert len(brief.encode()) < len(default.encode())
    assert "Return a detailed Markdown plan" in default
    assert "five short bullets" in brief and "Keep every stated criterion" in brief
    assert "write a detailed plan in this same response" in brief
    assert "out-of-order responses, reselection" in brief
    assert "belongs to the current options" in brief
    assert "A → B → A" in brief and "unit" in brief
    assert response_fields(request) == {"summary", "questions", "plan", "criteria"}


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
