"""Explicit project Git configuration, read-only tests and approved branch delivery."""

import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from dev_orchestra.application.errors import ApplicationError
from dev_orchestra.application.handoff import verify_branch
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.evidence import verified_candidate
from dev_orchestra.infrastructure.files import digest, write
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.git_credentials import CredentialVault, network_git
from dev_orchestra.infrastructure.locking import lock
from dev_orchestra.infrastructure.process import execution_scope
from dev_orchestra.infrastructure.process_owner import ensure_stopped
from dev_orchestra.infrastructure.store import now


def reject(message):
    raise ApplicationError("GIT_DELIVERY_BLOCKED", message)


def remote_address(git, name):
    if not name or name.startswith("-") or name not in git.call("remote").splitlines():
        reject("Push에 사용할 원격 저장소를 선택하세요.")
    urls = git.call("remote", "get-url", "--push", "--all", name).splitlines()
    if len(urls) != 1:
        reject("Push 주소가 하나인 원격 저장소를 사용하세요.")
    url = urls[0]
    try:
        parsed = urlsplit(url)
    except ValueError:
        reject("원격 주소 형식을 확인하세요.")
    # Embedded HTTPS credentials and arbitrary remote helpers are never accepted.
    if (
        any(c.isspace() or ord(c) < 32 for c in url)
        or url.startswith("-")
        or re.match(r"^[A-Za-z0-9+.-]+::", url)
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (
            parsed.scheme and parsed.scheme not in {"https", "http", "ssh", "file"} and "://" in url
        )
        or (
            parsed.scheme in {"http", "https"}
            and (parsed.username or parsed.password or parsed.query or parsed.fragment)
        )
    ):
        reject("인증 정보가 없는 HTTPS·SSH 또는 로컬 원격 주소를 사용하세요.")
    return url


class GitDeliveryService:
    def __init__(self, backend, vault=None, transport=None):
        self.backend = backend
        self.store = backend.store
        self.root = self.store.path.with_suffix(".git-settings")
        self.vault = vault or CredentialVault()
        self.transport = transport or network_git

    def path(self, pid):
        # Public IDs are resolved before they can influence filesystem paths.
        self.backend.project(pid)
        return self.root / (digest(pid.encode()) + ".json")

    def saved(self, pid):
        path = self.path(pid)
        return (
            json.loads(path.read_text())
            if path.exists()
            else {
                "remote": "",
                "account": "",
                "credential_id": None,
                "test": None,
            }
        )

    def project(self, pid):
        project = self.backend.project(pid)
        self.backend.verify_identity(project)
        return project

    def view(self, pid):
        project = self.project(pid)
        git = Git(Path(project["repository_path"]))
        settings = self.saved(pid)
        remotes = []
        for name in git.call("remote").splitlines():
            try:
                remotes.append({"name": name, "url": remote_address(git, name)})
            except ApplicationError:
                # Never return an unsafe URL (including embedded credentials).
                continue
        url = next((r["url"] for r in remotes if r["name"] == settings["remote"]), None)
        fingerprint = digest(json.dumps([settings, url], sort_keys=True).encode())
        test = settings.get("test")
        if test and test["url"] != url:
            test = None
        return {
            "remote": settings["remote"],
            "remote_url": url,
            "account": settings["account"],
            "token_registered": bool(settings.get("credential_id")),
            "version": fingerprint,
            "remotes": remotes,
            "test": test,
        }

    def save(self, pid, body):
        with lock(self.path(pid).with_suffix(".lock")):
            current = self.view(pid)
            if current["version"] != body.expected_version:
                raise ApplicationError("VERSION_CONFLICT", "Git 설정이 변경되었습니다.")
            project = self.project(pid)
            url = remote_address(Git(Path(project["repository_path"])), body.remote)
            settings = self.saved(pid)
            old = settings.get("credential_id")
            credential = old
            previous_token = (
                self.vault.get(old)
                if old and (body.token is not None or body.delete_token)
                else None
            )
            if body.token is not None:
                if not body.account or not url.startswith("https://"):
                    reject("토큰은 계정 ID와 HTTPS 원격 주소가 필요합니다.")
                credential = uuid.uuid4().hex
                self.vault.set(credential, body.token.get_secret_value())
            elif body.delete_token:
                credential = None
            elif old and (
                body.account != settings["account"]
                or body.remote != settings["remote"]
                or settings.get("credential_url") != url
            ):
                reject("계정·원격 변경 시 토큰을 교체하거나 삭제하세요.")
            value = {
                "remote": body.remote,
                "account": body.account,
                "credential_id": credential,
                "credential_url": url if credential else None,
                "test": None,
            }
            try:
                if old and old != credential:
                    self.vault.delete(old)
                write(self.path(pid), value)
            except BaseException:
                if previous_token is not None:
                    self.vault.set(old, previous_token)
                if credential and credential != old:
                    self.vault.delete(credential)
                raise
            return self.view(pid)

    def network(self, pid, url, args):
        settings = self.saved(pid)
        token = self.vault.get(settings["credential_id"]) if settings.get("credential_id") else None
        if settings.get("credential_id") and (
            not token or not url.startswith("https://") or settings.get("credential_url") != url
        ):
            reject("저장된 토큰 또는 HTTPS 원격 주소를 확인하세요.")
        # Callers validate repository identity before entering the network ownership scope.
        project = self.backend.project(pid)
        return self.transport(
            Path(project["repository_path"]), args, settings["account"], token, url=url
        )

    def test(self, pid, expected_version):
        directory = self.path(pid).with_suffix(".connection")
        with lock(self.path(pid).with_suffix(".lock")):
            ensure_stopped(directory)
            view = self.view(pid)
            if view["version"] != expected_version:
                raise ApplicationError("VERSION_CONFLICT", "Git 설정이 변경되었습니다.")
            if not view["remote_url"]:
                reject("원격 저장소를 선택하세요.")
            url = view["remote_url"]
            # receive-pack dry-run does not prove protected-branch write permission.
            # Only query refs, and explicitly leave push permission unknown.
            try:
                with execution_scope(directory):
                    result = self.network(pid, url, ["ls-remote", "--", url])
            except (ApplicationError, PipelineError, OSError):
                result = {
                    "status": "credential_error",
                    "message": "인증 설정 또는 OS 자격 증명 저장소를 확인하세요.",
                }
            settings = self.saved(pid)
            settings["test"] = {
                **result,
                "url": url,
                "checked_at": now(),
                "push_permission": "unknown",
            }
            write(self.path(pid), settings)
            return self.view(pid)

    def registered_run(self, rid):
        run = self.backend.registered_run(rid)
        with self.store.connection() as db:
            project_id = db.execute("SELECT project_id FROM runs WHERE id=?", (rid,)).fetchone()[0]
        return {**run, "project_id": project_id}

    def preview(self, rid):
        run = self.registered_run(rid)
        view = self.view(run["project_id"])
        return {
            "remote": view["remote"],
            "remote_url": view["remote_url"],
            "settings_version": view["version"],
            "branch": run["branch"],
            "approved_sha": run.get("final_sha") if run["state"] == "completed" else None,
            "result": run.get("push_result"),
        }

    def push(self, rid, body, expected_version):
        run = self.registered_run(rid)
        directory = Path(run["directory"])
        with lock(directory / "execution.lock"):
            run = self.registered_run(rid)
            if run["version"] != expected_version:
                raise ApplicationError("VERSION_CONFLICT", "Run이 변경되었습니다.")
            ensure_stopped(directory)
            if (
                run["state"] != "completed"
                or run.get("cleanup_requested")
                or run.get("deletion_pending")
            ):
                reject("최종 승인된 완료 작업만 Push할 수 있습니다.")
            branch, sha = run["branch"], run.get("final_sha")
            if (
                not sha
                or branch in {"main", "master", run["base"]}
                or not re.fullmatch(r"[0-9a-f]{40,64}", sha)
            ):
                reject("승인된 작업 브랜치만 Push할 수 있습니다.")
            Git(Path(run["repo"])).call("check-ref-format", "refs/heads/" + branch)
            try:
                verify_branch(run, sha, worktree=not run.get("worktree_removed", False))
            except (PipelineError, OSError):
                raise ApplicationError(
                    "PUSH_CANDIDATE_CHANGED", "승인 후 작업 브랜치가 변경되었습니다."
                ) from None
            try:
                verified_candidate(self.store, run, sha)
            except (PipelineError, OSError):
                raise ApplicationError(
                    "PUSH_EVIDENCE_INVALID", "승인 후보의 검증 근거를 확인할 수 없습니다."
                ) from None
            evidence = digest(json.dumps(run["test_result"], sort_keys=True).encode())
            with self.store.connection() as db:
                approval = db.execute(
                    "SELECT 1 FROM approvals WHERE run_id=? AND sha=? AND evidence=?",
                    (rid, sha, evidence),
                ).fetchone()
            if not approval:
                reject("최종 승인 근거가 일치하지 않습니다.")
            pid = run["project_id"]
            with lock(self.path(pid).with_suffix(".lock")):
                view = self.view(pid)
                if (
                    view["version"] != body.settings_version
                    or view["remote_url"] != body.remote_url
                    or sha != body.approved_sha
                    or branch != body.branch
                ):
                    reject("Push 대상이 변경되었습니다. 대상을 다시 확인하세요.")
                if not view["remote_url"]:
                    reject("프로젝트에서 원격 저장소를 설정하세요.")
                attempt = {
                    "status": "running",
                    "message": "Push 진행 중",
                    "remote": view["remote"],
                    "remote_url": view["remote_url"],
                    "branch": branch,
                    "sha": sha,
                    "checked_at": now(),
                }
                self.store.update(rid, push_result=attempt)
                try:
                    verify_branch(run, sha, worktree=not run.get("worktree_removed", False))
                    with execution_scope(directory):
                        result = self.network(
                            pid,
                            view["remote_url"],
                            [
                                "-c",
                                "push.followTags=false",
                                "-c",
                                "push.autoSetupRemote=false",
                                "-c",
                                "remote.pushDefault=",
                                "push",
                                "--no-mirror",
                                "--porcelain",
                                "--no-follow-tags",
                                "--recurse-submodules=no",
                                "--",
                                view["remote_url"],
                                sha + ":refs/heads/" + branch,
                            ],
                        )
                except (ApplicationError, PipelineError, OSError):
                    result = {
                        "status": "failed",
                        "message": "Push 인증 또는 연결을 확인한 뒤 재시도하세요.",
                    }
                finished = {**attempt, **result, "checked_at": now()}
                self.store.update(
                    rid,
                    push_result=finished,
                    push_attempts=[*run.get("push_attempts", []), finished],
                )
                return self.preview(rid)
