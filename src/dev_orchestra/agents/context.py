"""Role inputs omit already-applied patches; durable host artifacts remain complete."""

import json

from dev_orchestra.infrastructure.process import sanitize

DIAGNOSTIC_BUDGET = 20000


def execution_summary(execution):
    value = {key: execution.get(key) for key in ["status", "passed", "duration_seconds"]}
    value["commands"] = []
    remaining = 12000
    commands = execution.get("commands", [])
    for command in commands:
        row = {
            key: command.get(key)
            for key in ["command_id", "kind", "status", "passed", "total", "failed", "skipped"]
        }
        details = json.dumps(
            sanitize({key: command.get(key) for key in ["error", "failure_details", "failures"]}),
            ensure_ascii=False,
        )
        row["diagnostic"] = details[: max(0, min(remaining, 3000))]
        remaining -= len(row["diagnostic"])
        row["truncated"] = len(row["diagnostic"]) < len(details)
        value["commands"].append(row)
        if len(json.dumps(value, ensure_ascii=False)) > DIAGNOSTIC_BUDGET - 1000:
            value["commands"].pop()
            break
    value["omitted_commands"] = len(commands) - len(value["commands"])
    value["truncated"] = bool(value["omitted_commands"]) or any(
        c["truncated"] for c in value["commands"]
    )
    return value


def role_context(request):
    context = request.context
    role = request.role
    if role == "developer":
        context.pop("test_result", None)
        context.pop("review_result", None)
    else:
        tests = context.get("test_result")
        if tests:
            context["test_result"] = {
                "sha": tests.get("sha"),
                "execution": execution_summary(tests.get("execution", {})),
                "test_selection": tests.get("test_selection"),
                "evidence_reference": "test-result.json",
            }
        review = context.get("review_result")
        if review:
            context["review_result"] = {
                key: review.get(key) for key in ["sha", "issues", "issue_states"]
            }
    for key in ["approved_test_changes", "test_change_records"]:
        context[key] = [
            {k: v for k, v in record.items() if k not in {"patch", "result"}}
            for record in context.get(key, [])
        ]
    if context.get("open_issues") is not None:
        context.pop("review_result", None)
    context["review_scope"] = (
        "Review the cumulative Run changes and directly affected code. Read surrounding functions, "
        "callers, dependencies, contracts and tests as needed to judge regressions and requirements. "
        "Do not audit unrelated project code. Every prior unresolved host issue still requires an "
        "independent assessment; the last Fixer diff alone is insufficient. For affected tests, "
        "consider included/excluded commands and expansion reasons; never treat them as full-suite coverage."
    )
