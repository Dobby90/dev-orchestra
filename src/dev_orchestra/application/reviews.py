"""Host issue identity and explicit, SHA-bound independent reassessment."""

import json
import uuid
from collections import Counter
from difflib import SequenceMatcher

from dev_orchestra.agents.contracts import ResponseError
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import digest
from dev_orchestra.infrastructure.process import redact, sanitize
from dev_orchestra.infrastructure.store import now


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

    def fixer_issues(self, run):
        from dev_orchestra.application.results import Results

        issues = self.open(run["id"])
        assessed_steps = {i["assessed_step_id"] for i in issues if "assessed_step_id" in i}
        if not assessed_steps:
            return issues

        assessments = {}
        results = Results(self.store)
        for step in self.store.steps(run["id"]):
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
        return issues

    def apply(self, run, step_id, payload):
        sha = payload["sha"]
        existing = {i["issue_id"]: i for i in self.issues(run["id"])}
        seen = set()
        with self.store.connection() as db:
            for assessment in payload.get("assessments", []):
                key = assessment["issue_id"]
                if key not in existing or key in seen:
                    raise PipelineError("unknown or duplicate review assessment")
                seen.add(key)
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
                        "코드 수정 확인, 동일 SHA·revision 테스트 통과 근거 대기"
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
            severity_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
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
                    key=severity_order.get,
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
