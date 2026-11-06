"""Persistent project and submission services, without HTTP dependencies."""

import base64
import hashlib
import hmac
import json
import tempfile
import time
import uuid
from pathlib import Path

from dev_orchestra.application.agent_settings import AgentSettingsService, settings_fingerprint
from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.projects import ProjectService
from dev_orchestra.application.web_contracts import ProjectCreate, Proposal
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.store import now


def identifier(prefix):
    return prefix + "-" + uuid.uuid4().hex


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class BackendService:
    def __init__(self, runs, roots, key):
        self.runs, self.store, self.key = runs, runs.store, key
        self.projects = ProjectService(roots)

    def project(self, project_id):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise ApplicationError("PROJECT_NOT_FOUND", "project not found")
        value = dict(row)
        value["policy"] = json.loads(value["policy"])
        value["branch_prefix"] = self.runs.config.project.branch_prefix
        value.pop("identity")
        return value

    def verify_identity(self, project):
        info = self.projects.validate_repository(
            Path(project["repository_path"]), project["default_base_branch"]
        )
        with self.store.connection() as db:
            expected = json.loads(
                db.execute("SELECT identity FROM projects WHERE id=?", (project["id"],)).fetchone()[
                    0
                ]
            )
        stat = info.git_common_directory.stat()
        if expected != {"device": stat.st_dev, "inode": stat.st_ino}:
            raise ApplicationError("REPOSITORY_CHANGED", "repository identity changed")
        return info

    def policy_config(self, policy):
        config = AgentSettingsService(self.runs.config).apply(self.runs.config)
        config.tests = policy.tests.model_copy(deep=True)
        config.workflow.max_fix_attempts = policy.max_fix_attempts
        config.review.blocking_severities = policy.blocking_severities
        return self.projects.validate_policy(config)

    def validate_project(self, value):
        info = self.projects.validate_repository(
            Path(value.repository_path), value.default_base_branch
        )
        self.policy_config(value.policy)
        return info

    def register(self, value):
        info = self.validate_project(value)
        stamp = now()
        pid = identifier("prj")
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    pid,
                    value.name,
                    str(info.repository_path),
                    str(info.git_common_directory),
                    canonical(
                        {
                            "device": info.git_common_directory.stat().st_dev,
                            "inode": info.git_common_directory.stat().st_ino,
                        }
                    ),
                    value.default_base_branch,
                    value.policy.model_dump_json(),
                    0,
                    stamp,
                    stamp,
                ),
            )
            for row in db.execute("SELECT id,data FROM runs WHERE project_id IS NULL").fetchall():
                if json.loads(row["data"]).get("repo") == str(info.repository_path):
                    db.execute("UPDATE runs SET project_id=? WHERE id=?", (pid, row["id"]))
        return self.project(pid)

    def change_project(self, pid, version, patch=None):
        current = self.project(pid)
        merged = {
            k: current[k] for k in ["name", "repository_path", "default_base_branch", "policy"]
        }
        if patch is not None:
            changes = patch.model_dump(exclude_unset=True)
            if not changes or any(v is None for v in changes.values()):
                raise ApplicationError("VALIDATION_ERROR", "provide non-null project changes")
            merged.update(changes)
            self.validate_project(ProjectCreate.model_validate(merged))
        with self.store.transaction() as db:
            result = db.execute(
                "UPDATE projects SET name=?,default_base_branch=?,policy=?,version=version+1,updated_at=?,archived_at=? WHERE id=? AND version=?",
                (
                    merged["name"],
                    merged["default_base_branch"],
                    canonical(merged["policy"]),
                    now(),
                    current["archived_at"] if patch is not None else now(),
                    pid,
                    version,
                ),
            )
            if not result.rowcount:
                raise ApplicationError("VERSION_CONFLICT", "project version changed")
        return self.project(pid)

    def registered_run(self, rid):
        run = self.store.get(rid)
        with self.store.connection() as db:
            row = db.execute("SELECT project_id FROM runs WHERE id=?", (rid,)).fetchone()
        if not row["project_id"]:
            raise ApplicationError(
                "PROJECT_REQUIRED", "register and associate project before web commands"
            )
        p = self.project(row["project_id"])
        info = self.verify_identity(p)
        if (
            str(info.git_common_directory) != p["git_common_directory"]
            or run["repo"] != p["repository_path"]
        ):
            raise ApplicationError("REPOSITORY_CHANGED", "repository identity changed")
        return run

    def spec_text(self, spec):
        if spec.kind == "text":
            text = spec.text
        else:
            with self.store.connection() as db:
                row = db.execute("SELECT text FROM specs WHERE id=?", (spec.spec_id,)).fetchone()
            if not row:
                raise ApplicationError("SPEC_NOT_FOUND", "spec not found")
            text = row["text"]
        if not text.strip() or "\x00" in text or len(text.encode()) > 1048576:
            raise ApplicationError("VALIDATION_ERROR", "invalid PRD text")
        return text

    def save_spec(self, text, filename=None):
        # Same validation for uploaded and inline input.
        from dev_orchestra.application.web_contracts import TextSpec

        text = self.spec_text(TextSpec(kind="text", text=text))
        sid = identifier("spec")
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.store.connection() as db:
            db.execute(
                "INSERT INTO specs VALUES(?,?,?,?,?,?)",
                (sid, text, digest, len(text.encode()), filename, now()),
            )
        return {"id": sid, "sha256": digest, "bytes": len(text.encode())}

    def preview(self, proposal):
        p = self.project(proposal.project_id)
        self.verify_identity(p)
        if p["archived_at"]:
            raise ApplicationError("INVALID_STATE", "project archived")
        from dev_orchestra.application.web_contracts import Policy

        config = self.policy_config(Policy.model_validate(p["policy"]))
        config.project.default_base_branch = p["default_base_branch"]
        base = proposal.base_branch or p["default_base_branch"]
        info = self.projects.validate_repository(Path(p["repository_path"]), base)
        from dev_orchestra.application.validation import complete_run_validation

        config = complete_run_validation(config, info)
        rid = "run-" + uuid.uuid4().hex[:16]
        branch = proposal.feature_branch or config.project.branch_prefix + "/" + rid
        if not branch.startswith(config.project.branch_prefix + "/"):
            raise ApplicationError("VALIDATION_ERROR", "feature branch prefix required")
        git = Git(info.repository_path)
        git.call("check-ref-format", "--branch", branch)
        if (
            branch
            in git.call("for-each-ref", "--format=%(refname:short)", "refs/heads/").splitlines()
        ):
            raise ApplicationError("BRANCH_CONFLICT", "feature branch exists")
        text = self.spec_text(proposal.spec)
        from dev_orchestra.application.validation import policy_warnings

        value = {
            "run_id": rid,
            "branch": branch,
            "base_sha": info.base_sha,
            "project_version": p["version"],
            "agent_settings_hash": settings_fingerprint(config),
            "proposal_hash": hashlib.sha256(
                canonical(proposal.model_dump(mode="json")).encode()
            ).hexdigest(),
            "expires": int(time.time()) + 300,
        }
        raw = base64.urlsafe_b64encode(canonical(value).encode()).decode()
        token = raw + "." + hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()
        return {
            **value,
            "test_scope": proposal.test_scope,
            "test_scope_notice": (
                "모델이 변경 코드와 관련 테스트를 분석해 자동 선택합니다. 필요한 경우 관련 모듈로 범위를 확대하고 이유를 기록합니다."
                if proposal.test_scope == "affected"
                else "설정된 전체 테스트와 정적 검사를 실행합니다."
            ),
            "preview_token": token,
            "spec_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "validation_warnings": policy_warnings(config, info),
        }

    def submit(self, command, idem_key):
        proposal = Proposal.model_validate(command.model_dump(exclude={"preview_token"}))
        scope = "submit"
        key_hash = hashlib.sha256(idem_key.encode()).hexdigest()
        request_hash = hashlib.sha256(
            canonical(command.model_dump(mode="json")).encode()
        ).hexdigest()

        def replay(db):
            row = db.execute(
                "SELECT * FROM operations WHERE scope=? AND key_hash=?", (scope, key_hash)
            ).fetchone()
            if row:
                if row["request_hash"] != request_hash:
                    raise ApplicationError(
                        "IDEMPOTENCY_CONFLICT", "key reused for different request"
                    )
                return json.loads(row["result"])

        with self.store.connection() as db:
            old = replay(db)
        if old:
            return old
        try:
            raw, signature = command.preview_token.split(".")
            if not hmac.compare_digest(
                signature, hmac.new(self.key, raw.encode(), hashlib.sha256).hexdigest()
            ):
                raise ValueError()
            receipt = json.loads(base64.urlsafe_b64decode(raw))
        except (ValueError, TypeError, KeyError):
            raise ApplicationError("PREVIEW_STALE", "invalid preview") from None
        fresh = self.preview(proposal)
        if receipt["expires"] < time.time() or any(
            receipt.get(k) != fresh[k]
            for k in ["base_sha", "project_version", "proposal_hash", "agent_settings_hash"]
        ):
            raise ApplicationError("PREVIEW_STALE", "preview changed; request another preview")
        p = self.project(proposal.project_id)
        self.verify_identity(p)
        if (
            receipt["branch"]
            in Git(Path(p["repository_path"]))
            .call("for-each-ref", "--format=%(refname:short)", "refs/heads/")
            .splitlines()
        ):
            raise ApplicationError("BRANCH_CONFLICT", "feature branch exists")
        from dev_orchestra.application.web_contracts import Policy

        config = self.policy_config(Policy.model_validate(p["policy"]))
        if settings_fingerprint(config) != receipt["agent_settings_hash"]:
            raise ApplicationError("PREVIEW_STALE", "agent settings changed")
        config.project.default_base_branch = p["default_base_branch"]
        from dev_orchestra.application.contracts import CreateRun
        from dev_orchestra.application.service import RunService

        service = RunService(config)
        with tempfile.TemporaryDirectory(prefix="devorchestra-spec-") as temp:
            spec = Path(temp) / "spec.md"
            spec.write_text(self.spec_text(proposal.spec))
            data = service.prepare_run(
                CreateRun(
                    repo=Path(p["repository_path"]),
                    spec=spec,
                    base=proposal.base_branch,
                    max_fixes=proposal.max_fix_attempts,
                    test_scope=proposal.test_scope,
                ),
                run_id=receipt["run_id"],
                branch=receipt["branch"],
                resolve_settings=False,
            )
        if data["base_sha"] != receipt["base_sha"]:
            raise ApplicationError("PREVIEW_STALE", "base branch changed during submission")
        data["answers"] = proposal.additional_instructions
        op = identifier("op")
        job = identifier("job")
        sid = identifier("spec")
        from dev_orchestra.application.queue import QueueService

        online = QueueService(self.store).dispatcher_status()["alive"]
        result = {
            "schema_version": 1,
            "run_id": data["id"],
            "job_id": job,
            "operation_id": op,
            "status": "queued",
            "queued_reason": "waiting_capacity" if online else "dispatcher_offline",
            "run_version": 0,
        }
        with self.store.transaction() as db:
            old = replay(db)
            if old:
                return old
            if self.project(proposal.project_id)["version"] != receipt["project_version"]:
                raise ApplicationError("PREVIEW_STALE", "project changed")
            db.execute(
                "INSERT INTO specs VALUES(?,?,?,?,?,?)",
                (sid, data["spec"], data["spec_hash"], len(data["spec"].encode()), None, now()),
            )
            self.store.create(data)
            db.execute(
                "UPDATE runs SET project_id=?,spec_id=?,origin='api' WHERE id=?",
                (proposal.project_id, sid, data["id"]),
            )
            db.execute(
                "INSERT INTO jobs(id,run_id,kind,source,state,created_at) VALUES(?,?,'execute','api','queued',?)",
                (job, data["id"], now()),
            )
            db.execute(
                "INSERT INTO operations(id,scope,key_hash,request_hash,status,result,created_at,run_id,kind,accepted_version,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    op,
                    scope,
                    key_hash,
                    request_hash,
                    "applied",
                    canonical(result),
                    now(),
                    data["id"],
                    "submit",
                    0,
                    now(),
                ),
            )
            self.store._event(db, data["id"], "run.submitted", {"job_id": job})
        return result
