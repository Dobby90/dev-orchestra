"""Role inputs omit already-applied patches; durable host artifacts remain complete."""

import json

from dev_orchestra.infrastructure.process import sanitize

DIAGNOSTIC_BUDGET = 20000
COVERAGE_BUDGET = 4000


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


def coverage_summary(coverage):
    shown = []
    remaining = COVERAGE_BUDGET - 2  # JSON list brackets.
    for item in coverage:
        row = {key: item[key] for key in ("criterion_id", "command_ids")}
        remaining -= len(json.dumps(row, ensure_ascii=False).encode()) + 2
        if remaining < 0:
            break
        shown.append(row)
    return {"coverage": shown, "omitted_coverage": len(coverage) - len(shown)}


def role_context(request):
    context = request.context
    role = request.role
    if role == "developer":
        context.pop("test_result", None)
        context.pop("review_result", None)
    else:
        tests = context.get("test_result")
        if (
            role == "reviewer"
            and tests
            and (
                tests.get("sha") != request.input_sha
                or tests.get("revision", 0) != context.get("revision", 0)
            )
        ):
            context.pop("test_result")
            tests = None
        if tests:
            context["test_result"] = {
                "sha": tests.get("sha"),
                "revision": tests.get("revision", 0),
                "execution": execution_summary(tests.get("execution", {})),
                "test_selection": tests.get("test_selection"),
                "evidence_reference": "test-result.json",
            }
            if role == "reviewer":
                context["test_result"].update(coverage_summary(tests.get("coverage", [])))
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
