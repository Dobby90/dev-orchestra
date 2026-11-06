"""Read-only result views and host-owned, checksum-bound immutable evidence."""

import json
import uuid
from pathlib import Path

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process import sanitize
from dev_orchestra.infrastructure.store import now


class Results:
    def __init__(self, store):
        self.store = store

    def enabled(self):
        with self.store.connection() as db:
            return db.execute("SELECT version FROM schema_version").fetchone()[0] >= 5

    def register(self, run_id, path, kind, step_id=None):
        root = Path(self.store.get(run_id)["directory"])
        path = contained(root, str(Path(path).relative_to(root)))
        content = path.read_bytes()
        if len(content) > 16 * 1048576:
            raise PipelineError("artifact exceeds safety limit")
        relative = str(path.relative_to(root))
        sha = digest(content)
        with self.store.connection() as db:
            old = db.execute(
                "SELECT * FROM artifacts WHERE run_id=? AND relative_path=?", (run_id, relative)
            ).fetchone()
            if old:
                if old["sha256"] != sha:
                    raise PipelineError("immutable artifact changed")
                return old["id"]
            aid = "artifact-" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
                (aid, run_id, step_id, kind, relative, sha, len(content), now()),
            )
        return aid

    def artifact(self, run_id, aid):
        run = self.store.get(run_id)
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM artifacts WHERE id=? AND run_id=?", (aid, run_id)
            ).fetchone()
        if not row:
            raise ApplicationError("ARTIFACT_NOT_FOUND", "artifact not found")
        path = contained(Path(run["directory"]), row["relative_path"])
        if (
            not path.is_file()
            or path.stat().st_size != row["bytes"]
            or digest(path.read_bytes()) != row["sha256"]
        ):
            raise PipelineError("artifact missing or modified")
        return path, dict(row)

    def payload(self, run, step):
        try:
            root = Path(run["directory"])
            path = contained(root, str(Path(step["data"]["artifact"]).relative_to(root)))
            if digest(path.read_bytes()) != step["data"]["hash"]:
                raise PipelineError("result evidence modified")
            value = json.loads(path.read_text())
            if value["sha"] != step["output_sha"]:
                raise PipelineError("result SHA mismatch")
            return sanitize(value)
        except (OSError, KeyError, ValueError) as error:
            raise PipelineError("result evidence unavailable") from error

    def selected(self, run_id, role, step_id=None):
        run = self.store.get(run_id)
        steps = [s for s in self.store.steps(run_id) if s["role"] == role]
        selected = (
            next((s for s in steps if s["id"] == step_id), None)
            if step_id is not None
            else (
                next(
                    (s for s in reversed(steps) if s.get("revision", 0) == run.get("revision", 0)),
                    None,
                )
            )
        )
        if step_id is not None and selected is None:
            raise ApplicationError("STEP_NOT_FOUND", "step not found")
        history = [
            {
                k: s.get(k)
                for k in [
                    "id",
                    "revision",
                    "iteration",
                    "attempt",
                    "status",
                    "output_sha",
                    "started_at",
                    "finished_at",
                ]
            }
            for s in steps
        ]
        return run, selected, history

    def tests(self, run_id, step_id=None):
        run, step, history = self.selected(run_id, "tester", step_id)
        base = {
            "status": "not_run",
            "step_id": None,
            "sha": None,
            "revision": None,
            "duration_seconds": None,
            "total": None,
            "passed": None,
            "failed": None,
            "skipped": None,
            "commands": [],
            "failures": [],
            "history": history,
            "legacy_incomplete": True,
        }
        if not step:
            return base
        base.update(step_id=step["id"], sha=step["output_sha"], revision=step.get("revision", 0))
        if step["status"] == "running":
            return base | {"status": "running"}
        try:
            payload = self.payload(run, step)
        except PipelineError:
            return base | {"status": "unavailable"}
        execution = payload.get("execution")
        if not execution:
            return base | {"status": "unavailable"}
        commands = execution.get("commands", [])
        status = execution.get("status", "passed" if execution.get("passed") else "failed")
        complete = bool(commands) and all("duration_seconds" in c for c in commands)
        base.update(
            status=status,
            selection=payload.get("test_selection"),
            commands=commands,
            legacy_incomplete=not complete,
            duration_seconds=execution.get("duration_seconds"),
        )
        test_commands = [c for c in commands if c.get("kind", "test") == "test"]
        for field in ("total", "failed", "skipped"):
            if test_commands and all(isinstance(c.get(field), int) for c in test_commands):
                base[field] = sum(c[field] for c in test_commands)
        if all(base[field] is not None for field in ("total", "failed", "skipped")):
            base["passed"] = base["total"] - base["failed"] - base["skipped"]
        base["failures"] = [f for c in commands for f in c.get("failure_details", [])]
        return base

    def reviews(self, run_id, step_id=None):
        run, step, history = self.selected(run_id, "reviewer", step_id)
        base = {
            "status": "not_run",
            "step_id": None,
            "sha": None,
            "revision": None,
            "summary": "",
            "issues": [],
            "history": history,
            "legacy_incomplete": True,
            "blocking_severities": run.get("config", {})
            .get("review", {})
            .get("blocking_severities", []),
        }
        if not step:
            return base
        base.update(step_id=step["id"], sha=step["output_sha"], revision=step.get("revision", 0))
        if step["status"] == "running":
            return base | {"status": "running"}
        try:
            payload = self.payload(run, step)
        except PipelineError:
            return base | {"status": "unavailable"}
        issues = payload.get("issue_states")
        if issues is None:
            issues = [
                dict(
                    i,
                    issue_id=f"legacy-{step['id']}-{index}",
                    status="unresolved",
                    source_sha=step["output_sha"],
                    resolution_evidence=None,
                )
                for index, i in enumerate(payload.get("issues", []))
            ]
        if step_id is None and payload.get("schema_version") == 2:
            from dev_orchestra.application.reviews import Reviews

            current = {i["issue_id"]: i for i in Reviews(self.store).issues(run_id)}
            issues = [
                dict(
                    i,
                    status=current[i["issue_id"]]["status"],
                    resolution_evidence=None,
                    status_note="사용자 분류",
                )
                if i["issue_id"] in current and current[i["issue_id"]]["status"] != i["status"]
                else i
                for i in issues
            ]
        base.update(
            status="available",
            summary=payload.get("summary", ""),
            issues=issues,
            legacy_incomplete=payload.get("schema_version") != 2,
        )
        return base

    def save_snapshot(self, run_id, sha):
        if not self.enabled():
            return
        run = self.store.get(run_id)
        revision = run.get("revision", 0)
        with self.store.connection() as db:
            old = db.execute(
                "SELECT artifact_id FROM snapshots WHERE run_id=? AND revision=? AND candidate_sha=?",
                (run_id, revision, sha),
            ).fetchone()
        if old:
            self.artifact(run_id, old[0])
            return
        git = Git(Path(run["worktree"]))
        if git.sha() != sha:
            raise PipelineError("snapshot candidate changed")
        root = Path(run["directory"]) / "snapshots" / str(revision) / sha
        root.mkdir(parents=True, exist_ok=True)
        raw = git.call(
            "diff", "--name-status", "--no-renames", "-z", f"{run['base_sha']}...{sha}", "--"
        )
        fields = raw.split("\0")
        files = []
        if fields[-1] == "":
            fields.pop()
        if len(fields) % 2:
            raise PipelineError("invalid diff file list")
        if len(fields) > 2000:
            raise PipelineError("snapshot file count exceeds limit")
        for offset in range(0, len(fields), 2):
            status, path = fields[offset : offset + 2]
            fid = "file-" + digest((sha + "\0" + path).encode())[:24]
            stats = git.call(
                "diff", "--numstat", "--no-renames", f"{run['base_sha']}...{sha}", "--", path
            ).split("\t", 2)
            binary = bool(stats and stats[0] == "-")
            text = git.call(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                f"{run['base_sha']}...{sha}",
                "--",
                path,
            )
            data = sanitize(text).encode()
            truncated = len(data) > 1048576
            data = data[:1048576].decode("utf-8", "ignore")
            target = root / (fid + ".diff")
            write(target, data)
            aid = self.register(run_id, target, "diff")
            files.append(
                {
                    "file_id": fid,
                    "path": path,
                    "status": status,
                    "binary": binary,
                    "truncated": truncated,
                    "additions": None if binary else int(stats[0] or 0),
                    "deletions": None if binary else int(stats[1] or 0),
                    "artifact_id": aid,
                }
            )
        manifest = {
            "available": True,
            "revision": revision,
            "base_sha": run["base_sha"],
            "merge_base_sha": git.call("merge-base", run["base_sha"], sha),
            "candidate_sha": sha,
            "files": files,
        }
        path = root / "manifest.json"
        write(path, manifest)
        aid = self.register(run_id, path, "changes")
        with self.store.connection() as db:
            db.execute("INSERT INTO snapshots VALUES(?,?,?,?)", (run_id, revision, sha, aid))

    def changes(self, run_id, candidate_sha=None, revision=None):
        run = self.store.get(run_id)
        revision = run.get("revision", 0) if revision is None else revision
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM snapshots WHERE run_id=? ORDER BY revision,candidate_sha", (run_id,)
            ).fetchall()
        selected = next(
            (
                r
                for r in rows
                if r["revision"] == revision
                and (candidate_sha is None or r["candidate_sha"] == candidate_sha)
            ),
            None,
        )
        if not selected:
            if candidate_sha is not None:
                raise ApplicationError("SNAPSHOT_NOT_FOUND", "candidate snapshot unavailable")
            return {
                "available": False,
                "revision": revision,
                "base_sha": run.get("base_sha"),
                "merge_base_sha": None,
                "candidate_sha": None,
                "files": [],
                "history": [
                    {"revision": r["revision"], "candidate_sha": r["candidate_sha"]} for r in rows
                ],
            }
        path, _ = self.artifact(run_id, selected["artifact_id"])
        return json.loads(path.read_text()) | {
            "history": [
                {"revision": r["revision"], "candidate_sha": r["candidate_sha"]} for r in rows
            ]
        }

    def diff(self, run_id, file_id, candidate_sha, revision):
        view = self.changes(run_id, candidate_sha, revision)
        item = next((f for f in view["files"] if f["file_id"] == file_id), None)
        if not item:
            raise ApplicationError("FILE_NOT_FOUND", "file not found")
        path, _ = self.artifact(run_id, item["artifact_id"])
        return {
            **item,
            "base_sha": view["base_sha"],
            "candidate_sha": candidate_sha,
            "text": path.read_text(),
        }

    def save_report(self, run_id, text, version=None):
        if not self.enabled():
            return
        run = self.store.get(run_id)
        version = run["version"] if version is None else version
        root = Path(run["directory"])
        with lock(root / "report.lock", wait=True):
            with self.store.connection() as db:
                if db.execute(
                    "SELECT 1 FROM reports WHERE run_id=? AND version=?", (run_id, version)
                ).fetchone():
                    return
                relative = f"reports/{version}.md"
                old = db.execute(
                    "SELECT id FROM artifacts WHERE run_id=? AND relative_path=?",
                    (run_id, relative),
                ).fetchone()
            if old:
                self.artifact(run_id, old["id"])
                aid = old["id"]
            else:
                target = contained(root, relative)
                if not target.exists():
                    write(target, text)
                aid = self.register(run_id, target, "report")
            with self.store.connection() as db:
                db.execute("INSERT INTO reports VALUES(?,?,?)", (run_id, version, aid))

    def report(self, run_id):
        run = self.store.get(run_id)
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM reports WHERE run_id=? ORDER BY version DESC LIMIT 1", (run_id,)
            ).fetchone()
        if row:
            path, _ = self.artifact(run_id, row["artifact_id"])
            return {
                "markdown": path.read_text(),
                "artifact_id": row["artifact_id"],
                "generated_version": row["version"],
                "current_version": run["version"],
                "stale": row["version"] != run["version"],
            }
        if not run.get("directory"):
            raise ApplicationError("REPORT_NOT_FOUND", "report unavailable")
        path = contained(Path(run["directory"]), "final-report.md")
        if not path.is_file():
            raise ApplicationError("REPORT_NOT_FOUND", "report unavailable")
        return {
            "markdown": sanitize(path.read_text()),
            "artifact_id": None,
            "generated_version": None,
            "current_version": run["version"],
            "stale": True,
        }
