"""Explicit recovery under execution ownership; queries never reconcile files or Git."""

import json
from pathlib import Path

from dev_orchestra.application.test_changes import checksum
from dev_orchestra.domain.execution import WORKSPACE_V2, execution_contract
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.commit_messages import format_branch_commit
from dev_orchestra.infrastructure.design_images import design_image_binding, verified_run_images
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.filesystem_identity import directory_identity, same_directory
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.process_owner import ensure_stopped


class WorkspaceBlocked(PipelineError):
    """Preserved work cannot advance without verified ownership or repair budget."""


def _checkpoint_data(store, run):
    reference = run.get("workspace_checkpoint")
    if not reference:
        return None
    path = contained(Path(run["directory"]), reference["snapshot"])
    raw = path.read_bytes()
    if digest(raw) != reference["hash"]:
        raise PipelineError("workspace checkpoint checksum mismatch; files preserved")
    data = json.loads(raw)
    if any(
        reference[key] != data[key]
        for key in ["step_id", "role", "revision", "iteration", "input_sha"]
    ):
        raise PipelineError("workspace checkpoint binding mismatch")
    session = contained(Path(run["directory"]), reference["session"])
    if path.parent != session.parent or data["run_id"] != run["id"]:
        raise PipelineError("workspace checkpoint Run ownership mismatch")
    return session, data


def checkpoint_data(store, run):
    try:
        return _checkpoint_data(store, run)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise PipelineError("missing or invalid workspace checkpoint; files preserved") from error


def _inspect_workspace(store, run):
    from dev_orchestra.infrastructure.workspace import WorkspaceSession, workspace_policy_hash

    value = checkpoint_data(store, run)
    if value is None:
        return None
    path, data = value
    if execution_contract(run) != WORKSPACE_V2:
        raise PipelineError("workspace recovery contract mismatch")
    step = next((s for s in store.steps(run["id"]) if s["id"] == data["step_id"]), None)
    if (
        not step
        or step["status"] == "completed"
        or step["role"] != data["role"]
        or step["input_sha"] != data["input_sha"]
        or run["next_role"] != data["role"]
        or any(run.get(key, 0) != data[key] for key in ["iteration", "revision"])
        or any(run[key] != data[key] for key in ["repo", "worktree", "branch"])
    ):
        raise PipelineError("workspace recovery stage ownership mismatch; files preserved")
    policy = (
        workspace_policy_hash(run)
        if data.get("policy_hash_version") == 2
        else checksum(run["config"])
    )
    task = checksum(
        {
            key: run.get(key)
            for key in ["spec", "plan", "criteria", "answers", "directives", "clarifications"]
        }
        | design_image_binding(run)
    )
    if policy != data["policy_hash"] or task != data["task_hash"]:
        raise PipelineError("workspace recovery policy or requirements changed; files preserved")
    verified_run_images(run)
    git = Git(Path(run["worktree"]))
    worktree = directory_identity(run["worktree"])
    common = directory_identity(git.common())
    confirmation = run.get("workspace_identity_confirmation") or {}
    if confirmation.get("snapshot_hash") != run["workspace_checkpoint"]["hash"]:
        confirmation = {}
    if (
        not same_directory(confirmation.get("worktree", data["worktree_identity"]), worktree)
        or not same_directory(confirmation.get("common", data["common_identity"]), common)
        or digest((Path(run["worktree"]) / ".git").read_bytes()) != data["git_link_hash"]
        or str(git.common().resolve()) != data["common"]
        or Git(Path(run["repo"])).common().resolve() != git.common().resolve()
        or git.call("branch", "--show-current") != data["branch"]
    ):
        raise PipelineError("workspace recovery repository identity mismatch; files preserved")
    session = WorkspaceSession(path, store, _run_id=run["id"])
    if session.snapshot() != data["files"]:
        raise PipelineError("workspace files differ from host checkpoint; changes preserved")
    if git.call("diff", "--cached", "--name-only"):
        raise PipelineError("unexpected staged changes preserved during recovery")
    parent = data["input_sha"]
    commit_prefix = run.get("commit_prefix")
    for commit in data["commits"]:
        verify_intent(data, commit, git, parent, commit_prefix=commit_prefix)
        parent = commit["sha"]
    if parent != data["current_sha"]:
        raise PipelineError("workspace checkpoint chain mismatch")
    pending = []
    for intent_path in sorted(path.parent.glob("checkpoint-*.json")):
        intent = json.loads(intent_path.read_text())
        if any(c["request_id"] == intent["request_id"] for c in data["commits"]):
            continue
        # A recorded intent is recoverable only after a matching commit exists.
        candidate = intent.get("sha") or git.sha()
        intent = intent | {"sha": candidate}
        verify_intent(data, intent, git, parent, pending=True, commit_prefix=commit_prefix)
        pending.append((intent_path, intent))
        parent = candidate
    if (
        len(pending) > 1
        or git.sha() != parent
        or run["current_sha"]
        not in {
            data["current_sha"],
            parent,
            data["commits"][-1]["parent"] if data["commits"] else data["input_sha"],
        }
    ):
        raise PipelineError("unacknowledged HEAD differs from host commit intent; preserved")
    return session, pending


def inspect_workspace(store, run):
    try:
        return _inspect_workspace(store, run)
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise PipelineError(
            "missing or invalid workspace recovery evidence; files preserved"
        ) from error


def confirm_workspace_identity(store, run, previous, current):
    if not run.get("workspace_checkpoint"):
        return None
    try:
        _, data = checkpoint_data(store, run)
        worktree = directory_identity(run["worktree"])
        # Both directories must have been recorded on the device being confirmed.
        if (
            data["common_identity"] != previous
            or "volume_uuid" in data["worktree_identity"]
            or data["worktree_identity"]["device"] != previous["device"]
            or data["worktree_identity"]["inode"] != worktree["inode"]
            or worktree.get("volume_uuid") != current["volume_uuid"]
        ):
            return None
        confirmation = {
            "snapshot_hash": run["workspace_checkpoint"]["hash"],
            "common": current,
            "worktree": worktree,
        }
        inspect_workspace(store, run | {"workspace_identity_confirmation": confirmation})
        return confirmation
    except (PipelineError, OSError, KeyError, TypeError):
        return None


def verify_intent(data, intent, git, parent, *, pending=False, commit_prefix=None):
    args = {
        key: intent[key] for key in ["request_id", "expected_sha", "files", "message", "summary"]
    }
    marker = f"DevOrchestra-Checkpoint: {data['run_id']}:{data['step_id']}:{intent['request_id']}"
    message_format = intent.get("commit_message_format", 1)
    if message_format not in {1, 2, 3, 4} or (message_format == 1 and commit_prefix):
        raise PipelineError("unsupported workspace commit message format")
    message = (
        intent["message"].strip()
        if message_format == 1
        else format_branch_commit(
            intent["message"], data["branch"], version=message_format, commit_prefix=commit_prefix
        )
    )
    if (
        intent["parent"] != parent
        or intent["expected_sha"] != parent
        or intent["request_hash"] != checksum(args)
        or not intent["files"]
        or intent.get("policy_hash_version", 1) != data.get("policy_hash_version", 1)
        or pending
        and any(
            name not in data["files"] or data["files"][name]["hash"] != sha
            for name, sha in intent["files"].items()
        )
        or intent["commit_message"] != message + "\n\n" + marker
        or any(
            intent[key] != data[key]
            for key in [
                "run_id",
                "role",
                "iteration",
                "revision",
                "step_id",
                "input_sha",
                "policy_hash",
            ]
        )
        or git.call("rev-parse", intent["sha"] + "^") != parent
        or git.call("rev-parse", intent["sha"] + "^{tree}") != intent["tree"]
        or git.call("log", "-1", "--format=%B", intent["sha"]) != intent["commit_message"]
        or set(
            filter(
                None,
                git.call(
                    "diff-tree", "--no-commit-id", "--name-only", "-z", "-r", intent["sha"]
                ).split("\0"),
            )
        )
        != set(intent["files"])
    ):
        raise PipelineError("workspace commit intent ownership, parent or tree mismatch")


def recover_workspace(store, run):
    """Caller holds execution/repository locks and has verified worker/guardian exit."""
    ensure_stopped(Path(run["directory"]))
    value = inspect_workspace(store, run)
    if value is None:
        return None
    session, pending = value
    store.resume_step(session.data["step_id"])
    store.update(
        run["id"],
        {"developer": "developing", "fixer": "fixing", "tester": "testing"}[session.data["role"]],
    )
    session.data["worktree_identity"] = directory_identity(run["worktree"])
    session.data["common_identity"] = directory_identity(session.git.common())
    for path, intent in pending:
        write(path, intent)
        session.acknowledge(intent)
    store.update(run["id"], current_sha=session.data["current_sha"])
    session.data["testing"] = False
    session.data["continuing"] = True
    session.save()
    session.guard()
    return session
