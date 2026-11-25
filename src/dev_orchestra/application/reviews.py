"""Host issue identity and explicit, SHA-bound independent reassessment."""

import json
import re
import uuid
from collections import Counter
from difflib import SequenceMatcher

from dev_orchestra.agents.contracts import ResponseError
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.process import redact, sanitize
from dev_orchestra.infrastructure.store import now

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
VERIFICATION_PENDING_NOTE = "코드 수정 확인, 동일 SHA·revision 테스트 통과 근거 대기"


def verification_pending(issue, candidate_sha, review_step_id):
    return (
        issue["status"] == "unresolved"
        and issue.get("status_note") == VERIFICATION_PENDING_NOTE
        and issue.get("assessed_sha") == candidate_sha
        and issue.get("assessed_step_id") == review_step_id
    )


def cited_case(text, name):
    return bool(name and re.search(r"(?<![\w./-])" + re.escape(name) + r"(?![\w/-]|\.\w)", text))


def linked_test_failures(evidence, tests):
    linked = []
    for command in (tests or {}).get("execution", {}).get("commands", []):
        command_id = command.get("command_id")
        if not command_id or command.get("passed") is not False:
            continue
        details = command.get("failure_details") or [
            {"name": name} for name in command.get("failures", [])
        ]
        cases = [
            {"name": case["name"], "message": (case.get("message") or "")[:500]}
            for case in details
            if case.get("name")
            and any(cited_case(evidence, case.get(key)) for key in ("name", "file", "classname"))
        ]
        if cases or f"`{command_id}`" in evidence:
            linked.append(
                {
                    "command_id": command_id,
                    "status": command.get("status"),
                    "cases": cases[:10],
                    "omitted_cases": max(0, len(cases) - 10),
                    "error": (command.get("error") or "")[:500],
                }
            )
    return linked


def unresolved_groups(run, issues):
    links = run.get("review_issue_duplicates", {})
    groups = {}
    for index, issue in enumerate(issues):
        if issue.get("status", "unresolved") == "unresolved":
            issue_id = issue.get("issue_id", index)
            groups.setdefault(links.get(issue_id, issue_id), []).append(issue)
    return groups


def group_severity(group, blocking):
    return max(
        (issue["severity"] for issue in group),
        key=lambda severity: (severity in blocking, SEVERITY_ORDER[severity]),
    )


def _normalized_statement(text):
    return " ".join(text.split()).rstrip(".!?。")


def validate_review_findings(result, open_issues, candidate_sha):
    """Ask for a retry on overlapping claims while preserving independent findings."""
    existing_ids = {issue["issue_id"] for issue in open_issues}
    finding_ids = Counter(finding.id for finding in result.issues)
    distinctions = set()
    for item in result.issue_distinctions:
        pair = (item.finding_id, item.existing_issue_id)
        if (
            finding_ids[item.finding_id] != 1
            or item.existing_issue_id not in existing_ids
            or not item.reason.strip()
            or pair in distinctions
        ):
            raise ResponseError(
                "issue_distinctions must link a new finding and an open issue once with a concrete reason"
            )
        distinctions.add(pair)

    reassessments = {
        item.issue_id: _normalized_statement(redact(item.evidence))
        for item in result.assessments
        if item.candidate_sha == candidate_sha
    }
    overlaps = []
    for finding in result.issues:
        raw_description = redact(finding.description)
        description = _normalized_statement(raw_description)
        evidence = _normalized_statement(redact(finding.evidence))
        file = redact(finding.file)
        for issue in open_issues:
            issue_id = issue["issue_id"]
            same_file = file == issue["file"]
            existing_description = _normalized_statement(issue["description"])
            same_evidence = bool(evidence) and evidence in {
                _normalized_statement(issue["evidence"]),
                reassessments.get(issue_id),
            }
            if (same_file and raw_description == issue["description"]) or (
                description == existing_description and same_evidence
            ):
                overlaps.append(
                    f"{finding.id!r} repeats {issue_id!r}; use assessments for this ID or describe the independent behavior with distinct evidence"
                )
            elif (
                same_file
                and (
                    same_evidence
                    or description == existing_description
                    or (
                        min(len(description), len(existing_description)) >= 16
                        and SequenceMatcher(
                            None, description[:512], existing_description[:512]
                        ).ratio()
                        >= 0.88
                    )
                )
                and (finding.id, issue_id) not in distinctions
            ):
                overlaps.append(
                    f"{finding.id!r} overlaps {issue_id!r}; use assessments for this ID or give the independent failure and evidence in issue_distinctions"
                )
    if overlaps:
        raise ResponseError("overlapping review findings: " + "; ".join(overlaps))


def validate_assessment_ids(assessment_ids, known_ids):
    invalid = [
        issue_id
        for issue_id, count in Counter(assessment_ids).items()
        if issue_id not in known_ids or count > 1
    ]
    if invalid:
        raise ResponseError(
            "unknown or duplicate review assessment. Copy each issue_id exactly from "
            f"open_issues and assess it once. Invalid or repeated IDs: {invalid!r}"
        )


class Reviews:
    def __init__(self, store):
        self.store = store

    def issues(self, run_id):
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM review_issues WHERE run_id=? ORDER BY rowid", (run_id,)
            ).fetchall()
        return [
            json.loads(r["data"])
            | {"issue_id": r["id"], "severity": r["severity"], "status": r["status"]}
            for r in rows
        ]

    def open(self, run_id):
        return [i for i in self.issues(run_id) if i["status"] == "unresolved"]

    def selectable_issue_ids(self, run):
        if run["state"] != "waiting_approval" or run.get("review_contract") != 2:
            return set()
        blocking = run["config"]["review"]["blocking_severities"]
        return {
            issue["issue_id"]
            for group in unresolved_groups(run, self.open(run["id"])).values()
            if (severity := group_severity(group, blocking)) in {"medium", "low"}
            and severity not in blocking
            for issue in group
        }

    def link_duplicate(self, db, run, duplicate_id, representative_id, reason):
        if not reason.strip():
            raise PipelineError("review duplicate link requires a reason")
        if duplicate_id == representative_id:
            raise PipelineError("a review issue cannot be linked to itself")
        ids = {issue["issue_id"] for issue in self.issues(run["id"])}
        if not {duplicate_id, representative_id} <= ids:
            raise PipelineError("both review issue IDs must belong to this run")
        links = run.get("review_issue_duplicates", {})
        if representative_id in links:
            raise PipelineError("choose an existing representative ID as the target")
        if links.get(duplicate_id) == representative_id:
            return
        if duplicate_id in links:
            raise PipelineError("review issue is already linked to a different representative")
        # Joining two groups keeps each member linked directly to the chosen representative.
        updated = {
            issue_id: representative_id if target == duplicate_id else target
            for issue_id, target in links.items()
        } | {duplicate_id: representative_id}
        self.store.update(
            run["id"], expected_version=run["version"], review_issue_duplicates=updated
        )
        self.store._event(
            db,
            run["id"],
            "issue.duplicate_linked",
            {
                "duplicate_id": duplicate_id,
                "representative_id": representative_id,
                "reason": sanitize(reason),
            },
        )

    def fixer_issues(self, run):
        candidate_sha = run.get("current_sha") or run["base_sha"]
        return self.fixer_context(run, candidate_sha)["open_issues"]

    def fixer_context(self, run, candidate_sha):
        from dev_orchestra.application.results import Results

        issues = self.open(run["id"])
        request = run.get("review_fix_request") or {}
        selected = (
            request["issue_ids"]
            if request.get("revision") == run.get("revision", 0)
            and request.get("iteration") == run.get("iteration", 0)
            else []
        )
        if selected:
            links = run.get("review_issue_duplicates", {})
            targets = {links.get(issue_id, issue_id) for issue_id in selected}
            groups = unresolved_groups(run, issues)
            if targets - groups.keys():
                raise PipelineError("requested review issues changed before Fixer execution")
            issues = [
                issue for target, group in groups.items() if target in targets for issue in group
            ]
        steps = self.store.steps(run["id"])
        results = Results(self.store)
        revision = run.get("revision", 0)
        current_steps = {
            step["role"]: step
            for step in steps
            if step["role"] in {"tester", "reviewer"}
            and step["status"] == "completed"
            and step["output_sha"] == candidate_sha
            and step["revision"] == revision
        }
        tests = self._fixer_test_result(results, run, current_steps.get("tester"))
        self._include_latest_assessments(results, run, steps, issues)
        review_step = current_steps.get("reviewer", {})
        for issue in issues:
            latest = issue.get("latest_assessment", {})
            pending = latest.get("reviewer_status") == "resolved" and verification_pending(
                issue, candidate_sha, review_step.get("id")
            )
            issue["verification_pending"] = pending
            if not pending:
                if issue.get("status_note") == VERIFICATION_PENDING_NOTE:
                    issue["status_note"] = None
                remaining = (
                    latest["evidence"]
                    if latest.get("reviewer_status") == "unresolved"
                    else issue["evidence"]
                )
                issue["remaining_failure"] = remaining
                issue["test_failures"] = linked_test_failures(remaining, tests)

        blocking = run["config"]["review"]["blocking_severities"]
        groups = unresolved_groups(run, issues)
        previous = self._previous_fixer_outcomes(results, run, steps, groups)
        result = []
        for representative, group in groups.items():
            focus = max(
                group,
                key=lambda issue: (
                    not issue["verification_pending"],
                    issue["assessed_step_id"] if issue.get("latest_assessment") else -1,
                    issue["issue_id"] == representative,
                ),
            )
            item = dict(focus)
            if len(group) != 1 or focus["issue_id"] != representative:
                item.update(
                    {
                        "issue_id": representative,
                        "severity": group_severity(group, blocking),
                        "evidence_issue_id": focus["issue_id"],
                        "related_unresolved_issues": [
                            issue for issue in group if issue is not focus
                        ],
                    }
                )
            if not item["verification_pending"] and representative in previous:
                item["previous_fix"] = previous[representative]
            result.append(item)
        context = {"open_issues": result, "test_result": tests}
        if selected:
            context["requested_issue_ids"] = selected
        return context

    @staticmethod
    def _fixer_test_result(results, run, step):
        from dev_orchestra.application.test_selection import verify_selection, verify_tool_evidence

        if not step:
            return None
        try:
            payload = results.payload(run, step)
            if payload.get("revision", 0) != step["revision"]:
                return None
            verify_tool_evidence(run, step, payload)
            verify_selection(run, payload)
            return payload
        except (PipelineError, KeyError, OSError, ValueError):
            return None

    @staticmethod
    def _previous_fixer_outcomes(results, run, steps, groups):
        previous = {}
        links = run.get("review_issue_duplicates", {})
        pending = set(groups)
        for step in reversed(steps):
            if not pending:
                break
            if step["role"] != "fixer" or step["status"] != "completed":
                continue
            try:
                payload = results.payload(run, step)
            except PipelineError:
                continue
            if payload.get("revision", 0) != step["revision"]:
                continue
            # A current representative takes precedence over an alias in the same result.
            outcomes = {
                outcome["issue_id"]: outcome for outcome in payload.get("issue_outcomes", [])
            }
            for issue_id in sorted(outcomes, key=lambda key: key in links):
                representative = links.get(issue_id, issue_id)
                if representative not in pending:
                    continue
                previous[representative] = outcomes[issue_id] | {
                    "candidate_sha": step["output_sha"],
                    "revision": step["revision"],
                }
                pending.remove(representative)
        return previous

    @staticmethod
    def _include_latest_assessments(results, run, steps, issues):
        assessed_steps = {i["assessed_step_id"] for i in issues if "assessed_step_id" in i}
        if not assessed_steps:
            return

        assessments = {}
        for step in steps:
            if (
                step["id"] not in assessed_steps
                or step["role"] != "reviewer"
                or step["status"] != "completed"
            ):
                continue
            try:
                payload = results.payload(run, step)
            except PipelineError:
                continue
            if payload.get("revision", 0) != step["revision"]:
                continue
            for assessment in payload.get("assessments", []):
                if (
                    assessment["candidate_sha"] == step["output_sha"]
                    and assessment["evidence"].strip()
                ):
                    assessments[step["id"], assessment["issue_id"]] = {
                        "candidate_sha": step["output_sha"],
                        "revision": step["revision"],
                        "reviewer_status": assessment["status"],
                        "evidence": assessment["evidence"],
                    }
        for issue in issues:
            latest = assessments.get((issue.get("assessed_step_id"), issue["issue_id"]))
            if latest and latest["candidate_sha"] == issue.get("assessed_sha"):
                issue["latest_assessment"] = latest

    def apply(self, run, step_id, payload):
        sha = payload["sha"]
        existing = {i["issue_id"]: i for i in self.issues(run["id"])}
        assessments = payload.get("assessments", [])
        validate_assessment_ids((item["issue_id"] for item in assessments), existing)
        with self.store.connection() as db:
            for assessment in assessments:
                key = assessment["issue_id"]
                valid = assessment["candidate_sha"] == sha and bool(assessment["evidence"].strip())
                tests = run.get("test_result") or {}
                resolved = (
                    valid
                    and assessment["status"] == "resolved"
                    and tests.get("sha") == sha
                    and tests.get("revision", 0) == run.get("revision", 0)
                    and tests.get("execution", {}).get("passed") is True
                )
                state = "resolved" if resolved else "unresolved"
                evidence = sanitize(assessment["evidence"])
                db.execute(
                    "INSERT INTO issue_assessments VALUES(?,?,?,?,?)",
                    (key, step_id, state, evidence, sha),
                )
                data = existing[key] | {
                    "resolution_evidence": evidence if resolved else None,
                    "status_note": (
                        VERIFICATION_PENDING_NOTE
                        if valid and assessment["status"] == "resolved" and not resolved
                        else None
                    ),
                    "assessed_sha": sha,
                    "assessed_step_id": step_id,
                }
                db.execute(
                    "UPDATE review_issues SET status=?,data=? WHERE id=?",
                    (state, json.dumps(data), key),
                )
            for finding in payload["issues"]:
                fingerprint = digest(
                    json.dumps(
                        [finding["file"], finding["description"]], ensure_ascii=False
                    ).encode()
                )
                old = db.execute(
                    "SELECT * FROM review_issues WHERE run_id=? AND fingerprint=?",
                    (run["id"], fingerprint),
                ).fetchone()
                key = old["id"] if old else "issue-" + uuid.uuid4().hex
                severity = max(
                    [finding["severity"], old["severity"] if old else finding["severity"]],
                    key=SEVERITY_ORDER.get,
                )
                data = sanitize(finding) | {
                    "source_sha": sha,
                    "discovered_step_id": old["discovered_step_id"] if old else step_id,
                    "resolution_evidence": None,
                }
                if old:
                    db.execute(
                        "UPDATE review_issues SET status='unresolved',severity=?,data=? WHERE id=?",
                        (severity, json.dumps(data), key),
                    )
                else:
                    db.execute(
                        "INSERT INTO review_issues VALUES(?,?,?,?,?,?,?)",
                        (
                            key,
                            run["id"],
                            fingerprint,
                            step_id,
                            severity,
                            json.dumps(data),
                            "unresolved",
                        ),
                    )
            self.store._event(
                db, run["id"], "issue.assessed", {"step_id": step_id, "candidate_sha": sha}
            )
        return self.issues(run["id"])

    def disposition(self, run, issue_id, state, reason):
        issue = next((i for i in self.issues(run["id"]) if i["issue_id"] == issue_id), None)
        if issue is None:
            raise PipelineError("issue not found")
        if not reason.strip() or state not in {"unresolved", "dismissed"}:
            raise PipelineError("issue disposition requires a reason")
        if (
            state == "dismissed"
            and issue["severity"] in run["config"]["review"]["blocking_severities"]
        ):
            raise PipelineError("blocking issues cannot be dismissed")
        with self.store.connection() as db:
            step = db.execute(
                "SELECT max(id) FROM steps WHERE run_id=? AND role='reviewer'", (run["id"],)
            ).fetchone()[0]
            db.execute(
                "INSERT INTO issue_dispositions(issue_id,step_id,status,reason,created_at) VALUES(?,?,?,?,?)",
                (issue_id, step, state, reason, now()),
            )
            db.execute("UPDATE review_issues SET status=? WHERE id=?", (state, issue_id))
            self.store._event(
                db,
                run["id"],
                "issue.disposition",
                {"issue_id": issue_id, "status": state, "reason": sanitize(reason)},
            )
