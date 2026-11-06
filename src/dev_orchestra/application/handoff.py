"""Local branch handoff; inspection never changes refs or the original checkout."""

import shlex
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.git import Git


def repository_identity(repo):
    common = Git(Path(repo)).common().resolve(strict=True)
    stat = common.stat()
    return {"common": str(common), "device": stat.st_dev, "inode": stat.st_ino}


def verify_branch(run, sha, *, worktree=True):
    original = Git(Path(run["repo"]))
    identity = repository_identity(run["repo"])
    if run.get("repository_identity", identity) != identity:
        raise PipelineError("local handoff repository identity changed; files preserved")
    if original.call("show-ref", "--verify", "--hash", "refs/heads/" + run["branch"]) != sha:
        raise PipelineError("local handoff branch differs from approved candidate; files preserved")
    if worktree:
        git = Git(Path(run["worktree"]))
        if (
            repository_identity(run["worktree"]) != identity
            or git.call("symbolic-ref", "-q", "HEAD") != "refs/heads/" + run["branch"]
            or git.sha() != sha
        ):
            raise PipelineError("local handoff worktree or branch mismatch; files preserved")
    return sha


def handoff(run):
    if any(not run.get(key) for key in ("repo", "branch", "base_sha")):
        return None
    sha = run.get("final_sha")
    status, current = "pending", None
    if sha:
        try:
            current = Git(Path(run["repo"])).call(
                "show-ref", "--verify", "--hash", "refs/heads/" + run["branch"]
            )
            verify_branch(run, sha, worktree=not run.get("worktree_removed", False))
            status = "matching"
        except (PipelineError, OSError, KeyError):
            status = "changed"
    prefix = "git -C " + shlex.quote(run["repo"])
    ref = shlex.quote("refs/heads/" + run["branch"])
    base = shlex.quote(run["base_sha"])
    return {
        "repository": run["repo"],
        "branch": run["branch"],
        "approved_sha": sha if run["state"] == "completed" else None,
        "candidate_sha": sha,
        "current_branch_sha": current,
        "status": status,
        "commands": [
            prefix + " branch --list -- " + shlex.quote(run["branch"]),
            prefix + " log -5 --oneline " + ref + " --",
            prefix + " diff " + base + " " + ref + " --",
        ],
        "notice": (
            "Worktree 정리 후에도 로컬 브랜치·승인 커밋·보고서·검증 이력은 보존됩니다. "
            "정리 후 이 작업은 재개할 수 없습니다. 공통 Git 객체·보존 로그·캐시 전체를 삭제하지 않습니다. "
            "정리 전에도 위 명령으로 코드를 조회할 수 있습니다. "
            "원본 폴더에서 같은 브랜치로 전환하려면 Worktree 정리 후 미커밋 변경을 직접 확인하세요. "
            "Merge는 사용자가 직접 수행합니다. 작업 전체 삭제는 브랜치와 기록도 삭제합니다."
        ),
    }


def handoff_markdown(run):
    value = handoff(run)
    if value is None:
        return "\n\n## 로컬 브랜치 인계\n\n저장된 로컬 브랜치 정보가 없습니다.\n"
    return (
        "\n\n## 로컬 브랜치 인계\n\n"
        + f"원본 프로젝트: `{value['repository']}`\n\n로컬 브랜치: `{value['branch']}`\n\n"
        + f"최종 승인 SHA: `{value['approved_sha'] or '승인 전'}`\n\n"
        + f"현재 브랜치 SHA: `{value['current_branch_sha'] or '확인 불가'}` · {value['status']}\n\n"
        + "```sh\n"
        + "\n".join(value["commands"])
        + "\n```\n\n"
        + value["notice"]
    )
