"""Host-owned file editing and file-granular commit checkpoints for workspace-v2.

The model process stays read-only. Only these operations mutate the linked worktree.
The session and intent journal live outside it, under the host's execution lock.
"""

import json
import os
import re
import stat
from copy import deepcopy
from pathlib import Path

from dev_orchestra.agents.policy import in_paths
from dev_orchestra.application.test_changes import TestChangeService, checksum
from dev_orchestra.config import Config
from dev_orchestra.domain.execution import WORKSPACE_V2, execution_contract
from dev_orchestra.domain.models import (
    AgentResult,
    PipelineError,
    StopRequested,
    TestChange,
    WorkspaceResult,
)
from dev_orchestra.infrastructure.commit_messages import describe_commit, format_branch_commit
from dev_orchestra.infrastructure.containers import MANIFEST
from dev_orchestra.infrastructure.design_images import design_image_binding
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.filesystem_identity import directory_identity
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.process import redact, sanitize
from dev_orchestra.infrastructure.secret_files import (
    public_environment_example,
    readable_environment_example,
    secret_path,
)
from dev_orchestra.infrastructure.store import Store

DEPENDENCIES = {"node_modules", ".venv", "__pycache__", ".mypy_cache", ".ruff_cache"}
JEST_CONFIG = re.compile(r"jest\.config\.(?:[cm]?[jt]s|json)")
JEST_JUNIT = re.compile(r"""(['"])jest-junit\1""")
MAX_HASH_PATHS = 64


def jest_junit_references(name, content):
    if JEST_CONFIG.fullmatch(name):
        return {"reporter"} if JEST_JUNIT.search(content) else set()
    try:
        manifest = json.loads(content)
    except ValueError:
        return set()
    if not isinstance(manifest, dict):
        return set()
    references = set()
    for key in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
        section = manifest.get(key)
        if isinstance(section, dict) and "jest-junit" in section:
            references.add("dependency")
    config = manifest.get("jest")
    if isinstance(config, dict) and JEST_JUNIT.search(json.dumps(config.get("reporters"))):
        references.add("reporter")
    return references


def runs_jest(command, scripts):
    from dev_orchestra.application.frontend_build_scope import script_tokens
    from dev_orchestra.application.validation import script_identity

    if identity := script_identity(command.argv):
        argv = script_tokens(scripts.get(identity[1])) if isinstance(scripts, dict) else []
    else:
        argv = command.argv
        if Path(argv[0]).name in {"npm", "pnpm", "yarn"} and argv[1:2] == ["exec"]:
            argv = argv[4:] if argv[2:4] == ["--no", "--"] else argv[2:]
    return bool(argv and Path(argv[0]).name == "jest")


def workspace_policy_hash(run):
    config = deepcopy(run["config"])
    # Raising the existing repair budget is an authorized resume operation,
    # not a change to editing permissions, test commands or selection policy.
    config.get("workflow", {}).pop("max_fix_attempts", None)
    policy = {"config": config, "test_scope": run.get("test_scope", "full")}
    if run.get("commit_prefix"):
        policy["commit_prefix"] = run["commit_prefix"]
    return checksum(policy)


def dependency_path(name):
    return bool(set(Path(name).parts) & DEPENDENCIES)


class WorkspaceSession:
    def __init__(self, path, store, *, _run_id=None):
        self.path, self.store = Path(path), store
        self.data = json.loads(self.path.read_text()) if _run_id is None else {}
        self.run_id = _run_id or self.data["run_id"]
        self.reload()
        self.root = Path(self.data["worktree"]).resolve()
        self.git = Git(self.root)

    @classmethod
    def create(cls, store, run, request, step_id, directory):
        if execution_contract(run) != WORKSPACE_V2 or request.role not in {
            "developer",
            "fixer",
            "tester",
        }:
            raise PipelineError("workspace editing is restricted to developer/tester/fixer")
        git = Git(Path(run["worktree"]))
        if git.sha() != request.input_sha or git.call("diff", "--cached", "--name-only"):
            raise PipelineError("workspace session requires unchanged HEAD and empty index")
        outputs = Config.model_validate(run["config"]).tests.output_paths
        tracked = list(filter(None, git.call("ls-files", "-z").split("\0")))
        if any(in_paths(name, outputs) or dependency_path(name) for name in tracked):
            raise PipelineError("output paths hide tracked source files")
        data = {
            "run_id": run["id"],
            "worktree": run["worktree"],
            "repo": run["repo"],
            "branch": run["branch"],
            "common": str(git.common().resolve()),
            "worktree_identity": directory_identity(run["worktree"]),
            "common_identity": directory_identity(git.common()),
            "git_link_hash": digest((Path(run["worktree"]) / ".git").read_bytes()),
            "role": request.role,
            "iteration": request.iteration,
            "revision": run.get("revision", 0),
            "input_sha": request.input_sha,
            "current_sha": request.input_sha,
            "step_id": step_id,
            "policy_hash": workspace_policy_hash(run),
            "policy_hash_version": 2,
            "task_hash": checksum(
                {
                    k: run.get(k)
                    for k in ["spec", "plan", "criteria", "answers", "directives", "clarifications"]
                }
                | design_image_binding(run)
            ),
            "outputs": outputs,
            "files": {},
            "initial": {},
            "changes": {},
            "commits": [],
        }
        if request.role == "tester" and not run["config"]["tests"]["commands"]:
            from dev_orchestra.application.validation import run_test_policy, run_test_policy_hash

            run_test_policy(run)
            data["validation_hash"] = run_test_policy_hash(run)
        path = Path(directory) / "workspace-session.json"
        write(path, data)
        session = cls(path, store, _run_id=run["id"])
        snapshot = session.snapshot()
        if set(snapshot) != set(tracked):
            raise PipelineError(
                "existing untracked or ignored source preserved; inspect before editing"
            )
        # Existing ignored source is deliberately included: .gitignore is not authority.
        session.data["files"] = snapshot
        session.data["initial"] = snapshot.copy()
        session.save()
        session.guard()
        return session

    def save(self):
        # The DB points only to immutable snapshots. A crash cannot replace the
        # last acknowledged snapshot with an unacknowledged file write.
        raw = json.dumps(self.data, ensure_ascii=False, indent=2)
        sha = digest(raw.encode())
        snapshot = self.path.parent / ("workspace-state-" + sha + ".json")
        write(snapshot, raw)
        root = Path(self.store.get(self.run_id)["directory"])
        reference = {
            key: self.data[key] for key in ["step_id", "role", "revision", "iteration", "input_sha"]
        }
        reference.update(
            session=str(self.path.relative_to(root)),
            snapshot=str(snapshot.relative_to(root)),
            hash=sha,
        )
        self.store.update(self.run_id, workspace_checkpoint=reference)
        write(self.path, raw)

    def reload(self):
        from dev_orchestra.application.workspace_recovery import checkpoint_data

        run = self.store.get(self.run_id)
        reference = run.get("workspace_checkpoint")
        if reference and reference["session"] == str(self.path.relative_to(Path(run["directory"]))):
            _, self.data = checkpoint_data(self.store, run)
        elif reference:
            raise PipelineError("workspace session belongs to another active stage")
        else:
            self.data = json.loads(self.path.read_text())

    def workspace_state(self, paths=None):
        self.reload()
        self.guard()
        if paths is not None:
            if self.data["role"] != "tester" or not 1 <= len(paths) <= MAX_HASH_PATHS:
                raise PipelineError("workspace hash lookup range or role denied")
            hashes = {}
            for name in paths:
                path = contained(self.root, name)
                if (
                    str(path.relative_to(self.root)) != name
                    or in_paths(name, self.data["outputs"])
                    or dependency_path(name)
                    or secret_path(name)
                    and not readable_environment_example(path, name)
                ):
                    raise PipelineError("workspace hash lookup path denied")
                hashes[name] = self.data["files"].get(name, {}).get("hash")
            return {"candidate_sha": self.data["current_sha"], "files": hashes}
        return {
            "input_sha": self.data["input_sha"],
            "candidate_sha": self.git.sha(),
            "files": self.data["files"],
            "checkpoint_count": len(self.data["commits"]),
            "completed_units": [c["summary"][:1000] for c in self.data["commits"][-5:]],
            "test_evidence": self.data.get("test_evidence"),
            "metadata_only": self.data.get("metadata_only", False),
        }

    def capture_response(self, response):
        from pydantic import ValidationError

        from dev_orchestra.agents.contracts import ResponseError
        from dev_orchestra.domain.models import WorkspaceTestResult

        model = WorkspaceTestResult if self.data["role"] == "tester" else WorkspaceResult
        try:
            value = model.model_validate(response.model_dump(), strict=True).model_dump()
        except ValidationError as error:
            raise ResponseError("invalid workspace role metadata") from error
        if not value["summary"].strip():
            raise ResponseError("workspace response requires a nonempty summary")
        if sanitize(value) != value:
            raise PipelineError("sensitive workspace response denied; files preserved")
        self.reload()
        self.data["response"] = value
        self.save()

    def saved_response(self):
        from dev_orchestra.domain.models import WorkspaceTestResult

        model = WorkspaceTestResult if self.data["role"] == "tester" else WorkspaceResult
        return model.model_validate(self.data["response"], strict=True)

    def snapshot(self):
        git = self.source_git()
        names = git.call("ls-files", "-z", "--cached", "--others", "--", ".", *git.exclusions)
        result = {}
        for name in filter(None, names.split("\0")):
            # Git's literal inventory may contain untouched protected paths or symlinks.
            # Hash links themselves; edit_file still refuses every symlink component.
            path = self.root / name
            if path.is_symlink():
                result[name] = {"hash": digest(os.readlink(path).encode()), "mode": "symlink"}
                continue
            if not path.is_file() or not stat.S_ISREG(path.lstat().st_mode):
                raise PipelineError("deleted, symlink or special source file denied")
            result[name] = {
                "hash": digest(path.read_bytes()),
                "mode": stat.S_IMODE(path.stat().st_mode),
            }
        return result

    def source_git(self):
        return Git(
            self.root,
            [
                *self.data["outputs"],
                *[
                    pattern
                    for name in sorted(DEPENDENCIES)
                    for pattern in (name, "**/" + name + "/**")
                ],
            ],
        )

    def guard(self, *, staged=False, acknowledging=None):
        run = self.store.get(self.run_id)
        step = next(
            (s for s in self.store.steps(self.run_id) if s["id"] == self.data["step_id"]), None
        )
        if (
            not step
            or step["status"] != "running"
            or step["role"] != self.data["role"]
            or step["input_sha"] != self.data["input_sha"]
            or run["state"] not in {"developing", "fixing", "testing"}
        ):
            raise PipelineError("workspace stage ownership expired")
        if (
            execution_contract(run) != WORKSPACE_V2
            or any(run.get(key, 0) != self.data[key] for key in ["iteration", "revision"])
            or run["next_role"] != self.data["role"]
            or run["current_sha"]
            not in {
                self.data["current_sha"],
                acknowledging["parent"] if acknowledging else self.data["current_sha"],
            }
            or run["worktree"] != self.data["worktree"]
            or Path(run["worktree"]).resolve() != self.root
            or run["branch"] != self.data["branch"]
            or run["repo"] != self.data["repo"]
            or (
                workspace_policy_hash(run)
                if self.data.get("policy_hash_version") == 2
                else checksum(run["config"])
            )
            != self.data["policy_hash"]
            or checksum(
                {
                    k: run.get(k)
                    for k in ["spec", "plan", "criteria", "answers", "directives", "clarifications"]
                }
                | design_image_binding(run)
            )
            != self.data["task_hash"]
            or (
                self.data["role"] == "tester"
                and not run["config"]["tests"]["commands"]
                and self.data.get("validation_hash")
                != (run.get("candidate_validation") or {}).get("hash")
            )
        ):
            raise PipelineError("workspace Run ownership or policy changed")
        if run["control"]:
            raise PipelineError("workspace Run stopped")
        if (
            self.git.call("branch", "--show-current") != self.data["branch"]
            or str(self.git.common().resolve()) != self.data["common"]
            or Git(Path(run["repo"])).common().resolve() != self.git.common().resolve()
            or self.git.sha()
            != (acknowledging["sha"] if acknowledging else self.data["current_sha"])
        ):
            raise PipelineError("workspace branch, repository or HEAD changed")
        if not staged and self.git.call("diff", "--cached", "--name-only"):
            raise PipelineError("unexpected staged changes preserved")
        if self.snapshot() != self.data["files"]:
            raise PipelineError("unrecognized workspace changes preserved")
        return run

    def prepare_environment(self):
        """Prepare a declared container toolchain and acknowledge real generated lockfiles."""
        self.reload()
        self.guard()
        if self.data["role"] not in {"developer", "fixer"} or self.data.get("metadata_only"):
            raise PipelineError(
                "environment preparation is restricted to active code-changing roles"
            )
        if self.data.get("testing"):
            raise PipelineError("environment preparation is already in progress")
        from dev_orchestra.infrastructure.containers import prepare_dependencies, specification

        if specification(self.root) is None:
            raise PipelineError(
                "declare devorchestra.environment.json before preparing the environment"
            )
        self.data["testing"] = True
        self.save()
        try:
            prepared = prepare_dependencies(
                self.root,
                Config.model_validate(self.store.get(self.run_id)["config"]).tests,
                self.path.parent / "environment",
                lambda: self.store.get(self.run_id)["control"],
                generate_locks=True,
            )
        finally:
            self.reload()
            self.data["testing"] = False
            self.save()
        self.guard()
        generated = []
        for name, content in prepared["generated_lockfiles"].items():
            current = self.data["files"].get(name)
            self.edit_file(name, current["hash"] if current else None, content)
            generated.append({"file": name, "sha256": digest(content.encode())})
        return {
            "image_id": prepared["image_id"],
            "configuration_hash": prepared["configuration_hash"],
            "generated_lockfiles": generated,
            "summary": "도구·의존성 준비 완료. 테스트 통과 근거는 이후 Tester가 수집합니다.",
        }

    def edit_file(self, file, expected_hash, content, test_change=None):
        """Replace one UTF-8 file, with CAS and requirement-bound test authorization."""
        self.reload()
        self.guard()
        if self.data.get("metadata_only"):
            raise PipelineError("metadata repair cannot edit source files")
        if self.data.get("testing"):
            raise PipelineError("test execution is in progress; edits are locked")
        path = contained(self.root, file)
        if str(path.relative_to(self.root)) != file or any(c.isspace() for c in file):
            raise PipelineError("unsupported workspace path")
        if secret_path(file) and not public_environment_example(file, content):
            raise PipelineError("secret file edit denied")
        if in_paths(file, self.data["outputs"]) or dependency_path(file):
            raise PipelineError("output file edits denied")
        current = self.data["files"].get(file)
        if expected_hash != (current["hash"] if current else None):
            raise PipelineError("file content checksum mismatch")
        if not isinstance(content, str) or "\0" in content or redact(content) != content:
            raise PipelineError("binary or sensitive content denied")
        if current:
            try:
                path.read_bytes().decode("utf-8")
            except UnicodeError as error:
                raise PipelineError("binary source edits denied") from error
        run = self.store.get(self.run_id)
        tests = Config.model_validate(run["config"]).tests
        existing_test = file in self.data["initial"] and in_paths(file, tests.write_paths)
        if self.data["role"] == "tester" and not in_paths(file, tests.write_paths):
            raise PipelineError("test agent attempted production change")
        self._check_validation_edit(run, tests.commands, path, current, content)
        if existing_test:
            if not test_change:
                raise PipelineError("existing tests require requirement-bound change evidence")
            change = TestChange.model_validate(test_change)
            if change.file != file:
                raise PipelineError("test change file mismatch")
            TestChangeService(self.store).explanations(
                run, AgentResult(summary="workspace edit", test_changes=[change]), [file]
            )
            if sanitize(test_change) != test_change:
                raise PipelineError("sensitive test change evidence denied")
            self.data["changes"][file] = change.model_dump()
        elif test_change:
            raise PipelineError("test change evidence must describe an existing test")
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replacement also avoids mutating any external hardlink target.
        write(path, content)
        os.chmod(path, current["mode"] if current else 0o644)
        self.data["files"][file] = {
            "hash": digest(content.encode()),
            "mode": stat.S_IMODE(path.stat().st_mode),
        }
        self.save()
        return {"file": file, "hash": self.data["files"][file]["hash"]}

    def _check_validation_edit(self, run, commands, path, current, content):
        adds_docker = not current and path == self.root / MANIFEST
        adds_jest_junit = (
            path.name == "package.json" or JEST_CONFIG.fullmatch(path.name)
        ) and jest_junit_references(path.name, content) - jest_junit_references(
            path.name, path.read_text() if current else ""
        )
        if not (adds_docker or adds_jest_junit):
            return
        if not commands:
            from dev_orchestra.application.contracts import RepositoryInfo
            from dev_orchestra.application.validation import detect_validation

            info = RepositoryInfo(
                repository_path=self.root,
                git_common_directory=self.git.common(),
                base_branch=run["base"],
                base_sha=self.git.sha(),
            )
            detected = detect_validation(info, supported_only=True)
            commands = {item.id: item.command for item in detected.candidates}
        if adds_docker:
            from dev_orchestra.infrastructure.containers import container_node_projects

            container_node_projects(self.root, commands, include_root=True)
        if adds_jest_junit:
            from dev_orchestra.infrastructure.dependencies import read_package
            from dev_orchestra.infrastructure.runtime_adapters import jest_json_path

            package_dir = path.parent.relative_to(self.root).as_posix()
            test_commands = [
                command
                for command in commands.values()
                if command.kind == "test" and command.working_directory == package_dir
            ]
            reports = [(cmd, jest_json_path(self.root, cmd)) for cmd in test_commands]
            if any(report for _, report in reports):
                manifest = contained(path.parent, "package.json")
                scripts = read_package(manifest).get("scripts", {}) if manifest.is_file() else {}
                if not any(not report and runs_jest(cmd, scripts) for cmd, report in reports):
                    raise PipelineError(
                        f"{package_dir}: the host already converts the Jest JSON report to JUnit; "
                        "keep the existing Jest configuration without adding jest-junit"
                    )

    def changed_files(self):
        # No exclude-standard: ignored new source files must still be committed.
        names = self.git.call(
            "ls-files",
            "-z",
            "--modified",
            "--others",
            "--",
            ".",
            *self.source_git().exclusions,
        )
        return sorted(set(filter(None, names.split("\0"))))

    def checkpoint_commit(self, request_id, expected_sha, files, message, summary, *, _host=False):
        self.reload()
        if self.data.get("metadata_only") and not _host:
            raise PipelineError("metadata repair cannot request commits")
        if self.data.get("testing"):
            raise PipelineError("test execution is in progress; commits are locked")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", request_id):
            raise PipelineError("invalid checkpoint request ID")
        args = {
            "request_id": request_id,
            "expected_sha": expected_sha,
            "files": files,
            "message": message,
            "summary": summary,
        }
        if sanitize(args) != args or not summary.strip() or not re.search("[가-힣]", message):
            raise PipelineError("checkpoint requires Korean message and non-sensitive summary")
        intent_path = self.path.parent / ("checkpoint-" + request_id + ".json")
        if intent_path.exists():
            intent = json.loads(intent_path.read_text())
            if intent["request_hash"] != checksum(args):
                raise PipelineError("checkpoint request ID reused with different arguments")
            if not intent.get("sha"):
                # Recover only an exact parent/tree/identity match after commit response loss.
                sha = self.git.sha()
                if (
                    self.git.call("rev-parse", "HEAD^") != intent["parent"]
                    or self.git.call("rev-parse", "HEAD^{tree}") != intent["tree"]
                    or self.git.call("log", "-1", "--format=%B") != intent["commit_message"]
                ):
                    raise PipelineError("unacknowledged checkpoint requires inspection")
                intent["sha"] = sha
                write(intent_path, intent)
            known = any(c["request_id"] == request_id for c in self.data["commits"])
            if known:
                self.guard()
            else:
                if self.data["current_sha"] not in {intent["parent"], intent["sha"]}:
                    raise PipelineError("checkpoint is not a continuation of this stage")
                self.guard(acknowledging=intent)
            if (
                self.git.call("rev-parse", intent["sha"] + "^") != intent["parent"]
                or self.git.call("rev-parse", intent["sha"] + "^{tree}") != intent["tree"]
                or self.git.call("log", "-1", "--format=%B", intent["sha"])
                != intent["commit_message"]
            ):
                raise PipelineError("checkpoint evidence differs from committed history")
            self.acknowledge(intent)
            self.guard()
            return {"sha": intent["sha"], "parent": intent["parent"], "tree": intent["tree"]}
        run = self.guard()
        # Use the Run's stored prefix so changes in startup config do not affect it.
        branch_prefix = run["config"].get("project", {}).get("branch_prefix")
        message_format = {"feature": 3, "features": 4}.get(branch_prefix, 2)
        formatted_message = format_branch_commit(
            message,
            self.data["branch"],
            version=message_format,
            commit_prefix=run.get("commit_prefix"),
        )
        if expected_sha != self.data["current_sha"]:
            raise PipelineError("checkpoint expected HEAD mismatch")
        changed = self.changed_files()
        if not files or not set(files) <= set(changed):
            raise PipelineError("checkpoint selection contains unchanged or unknown files")
        if any(self.data["files"][name]["hash"] != value for name, value in files.items()):
            raise PipelineError("checkpoint selected content checksum mismatch")
        # Every pending change must have gone through edit_file, not just selected files.
        for name in changed:
            if name not in self.data["files"]:
                raise PipelineError("unrecognized checkpoint file")
        tree = self.git.stage_selected(list(files))
        commit_message = (
            formatted_message
            + "\n\nDevOrchestra-Checkpoint: "
            + self.run_id
            + ":"
            + str(self.data["step_id"])
            + ":"
            + request_id
        )
        intent = args | {
            **{
                key: self.data[key]
                for key in [
                    "run_id",
                    "role",
                    "iteration",
                    "revision",
                    "step_id",
                    "input_sha",
                    "policy_hash",
                    "task_hash",
                ]
            },
            "policy_hash_version": self.data.get("policy_hash_version", 1),
            "request_hash": checksum(args),
            "commit_message_format": message_format,
            "parent": expected_sha,
            "tree": tree,
            "commit_message": commit_message,
            "test_changes": [
                self.data["changes"][name] for name in files if name in self.data["changes"]
            ],
        }
        write(intent_path, intent)
        self.guard(staged=True)
        intent["sha"] = self.git.commit_selected(commit_message, expected_sha, tree)
        write(intent_path, intent)
        self.acknowledge(intent)
        self.guard()
        return {"sha": intent["sha"], "parent": expected_sha, "tree": tree}

    def acknowledge(self, intent):
        if not any(c["request_id"] == intent["request_id"] for c in self.data["commits"]):
            self.data["commits"].append(intent)
            self.data["current_sha"] = intent["sha"]
            self.save()
        # Duplicate older requests must never roll back the last acknowledged HEAD.
        self.store.update(self.run_id, current_sha=self.data["current_sha"])

    def test_policy(self):
        from dev_orchestra.application.test_selection import select_tests

        self.reload()
        self.guard()
        if self.data["role"] != "tester":
            raise PipelineError("test tools are restricted to tester")
        if self.data.get("test_evidence") and not self.changed_files():
            value = self.read_test_payload()
            if value["sha"] == self.git.sha():
                self.validate_test_payload(value)
                return value["selection"]
        return select_tests(self.store.get(self.run_id), self.root, self.git.sha())

    def run_tests(self, command_ids, test_plan=None, commit_message=""):
        from dev_orchestra.application.automatic_tests import (
            automatic,
            deferred_builds,
            prepare_selected_only,
        )
        from dev_orchestra.application.test_selection import select_tests, selected_config
        from dev_orchestra.infrastructure.gradle_preparation import (
            observe_preparation,
            preparation_control,
        )
        from dev_orchestra.infrastructure.test_runner import TestRunner

        self.reload()
        self.guard()
        if self.data.get("metadata_only"):
            raise PipelineError("metadata repair must reuse host test evidence")
        if self.data.get("testing"):
            raise PipelineError("test execution is already in progress")
        if self.data["role"] != "tester":
            raise PipelineError("test tools are restricted to tester")
        pending = self.changed_files()
        if pending:
            self.checkpoint_commit(
                "tests-" + str(len(self.data["commits"])),
                self.git.sha(),
                {name: self.data["files"][name]["hash"] for name in pending},
                describe_commit(commit_message, "", pending),
                "테스트 변경 파일: " + ", ".join(pending),
            )
        run = self.store.get(self.run_id)
        selection = select_tests(
            run,
            self.root,
            self.git.sha(),
            plan=[item.model_dump() if hasattr(item, "model_dump") else item for item in test_plan]
            if test_plan is not None
            else None,
        )
        if automatic(run) and "selection_hash" not in selection:
            raise PipelineError("automatic test selection requires test_plan")
        if set(command_ids) != set(selection["included"]) or len(command_ids) != len(
            set(command_ids)
        ):
            raise PipelineError("required command IDs: " + ", ".join(selection["included"]))
        if self.data.get("test_evidence"):
            previous = self.read_test_payload()
            if previous["selection"] == selection:
                self.validate_test_payload(previous)
                return self.test_summary(previous)
        import uuid

        evidence_id = "test-" + uuid.uuid4().hex
        directory = self.path.parent / evidence_id
        preflight = Path(run["directory"]) / "preflight"
        runner = getattr(self, "_test_runner", None) or TestRunner(preflight=preflight)
        tests = selected_config(run, selection)

        def stop():
            return self.store.read_control(self.run_id)

        self.data["testing"] = True
        self.save()
        try:
            if prepare_selected_only(run):
                with observe_preparation(
                    lambda progress: self.store.gradle_progress(self.run_id, progress), finish=True
                ):
                    runner.prepare(self.root, tests, preflight, preparation_control(stop))
                if action := stop():
                    raise StopRequested(action)
                self.guard()
            elif deferred := deferred_builds(run, self.git.sha(), tests):
                runner.prepare(
                    self.root,
                    tests.model_copy(
                        update={"commands": {key: tests.commands[key] for key in deferred}}
                    ),
                    preflight,
                    stop,
                )
                self.guard()
            execution = runner.run(self.root, tests, directory, stop)
        except StopRequested:
            raise
        except (PipelineError, OSError) as error:
            execution = {
                "passed": False,
                "status": "unavailable",
                "commands": [
                    {
                        "command_id": key,
                        "kind": command.kind,
                        "argv": command.argv,
                        "status": "unavailable",
                        "passed": False,
                        "error": sanitize(str(error)),
                    }
                    for key, command in tests.commands.items()
                ],
            }
        finally:
            self.reload()
            self.data["testing"] = False
            self.save()
        self.guard()
        value = {
            "evidence_id": evidence_id,
            "sha": self.git.sha(),
            "revision": run.get("revision", 0),
            "selection": selection,
            "execution": execution,
        }
        path = directory / "evidence.json"
        evidence_hash = write(path, value)
        self.data["test_evidence"] = {"id": evidence_id, "hash": evidence_hash}
        self.save()
        self.validate_test_payload(value)
        return self.test_summary(value)

    def read_test_payload(self):
        evidence = self.data.get("test_evidence")
        if not evidence:
            raise PipelineError("tester must run host tests and inspect actual results")
        raw = contained(self.path.parent, evidence["id"] + "/evidence.json").read_bytes()
        if digest(raw) != evidence["hash"]:
            raise PipelineError("test evidence checksum mismatch")
        return json.loads(raw)

    def validate_test_payload(self, value):
        from dev_orchestra.application.test_selection import select_tests, verify_selection

        if (
            value["sha"] != self.git.sha()
            or self.changed_files()
            or value["selection"]
            != select_tests(
                self.store.get(self.run_id),
                self.root,
                self.git.sha(),
                plan=value["selection"].get("test_plan"),
            )
        ):
            raise PipelineError("test evidence is stale; run tests on the final candidate")
        verify_selection(
            self.store.get(self.run_id),
            {
                "sha": value["sha"],
                "revision": value["revision"],
                "test_selection": value["selection"],
                "execution": value["execution"],
            },
        )
        for command in value["execution"].get("commands", []):
            if (
                command.get("junit_path")
                and digest(Path(command["junit_path"]).read_bytes()) != command["junit_hash"]
            ):
                raise PipelineError("JUnit evidence checksum mismatch")

        from dev_orchestra.application.test_selection import verify_tool_evidence

        verify_tool_evidence(
            self.store.get(self.run_id),
            {"data": {"artifact": str(self.path.parent / "result.json")}},
            {
                "sha": value["sha"],
                "revision": value["revision"],
                "test_selection": value["selection"],
                "execution": value["execution"],
                "test_evidence_reference": self.data["test_evidence"],
            },
        )

    def load_test_result(self, response):
        self.reload()
        self.guard()
        value = self.read_test_payload()
        if response.test_evidence_id != value["evidence_id"]:
            from dev_orchestra.agents.contracts import ResponseError

            raise ResponseError("tester must reference the actual host test evidence")
        self.validate_test_payload(value)
        return value

    @staticmethod
    def test_summary(value):
        from dev_orchestra.agents.context import execution_summary

        return {
            "evidence_id": value["evidence_id"],
            "sha": value["sha"],
            "selection": value["selection"],
            "execution": execution_summary(value["execution"]),
        }

    def read_test_evidence(self, evidence_id, offset=0, limit=8000, command_id=None):
        self.reload()
        self.guard()
        if (
            self.data["role"] != "tester"
            or not self.data.get("test_evidence")
            or self.data["test_evidence"]["id"] != evidence_id
            or offset < 0
            or not 1 <= limit <= 8000
        ):
            raise PipelineError("test evidence range or ownership denied")
        value = self.read_test_payload()
        result = {"evidence_id": evidence_id}
        if command_id is not None:
            if value["evidence_id"] != evidence_id:
                raise PipelineError("test evidence identity mismatch")
            self.validate_test_payload(value)
            command = next(
                (
                    item
                    for item in value["execution"]["commands"]
                    if item["command_id"] == command_id
                ),
                None,
            )
            if command is None:
                raise PipelineError("command was not executed for this test evidence")
            result.update(
                sha=value["sha"],
                revision=value["revision"],
                command={
                    key: command.get(key)
                    for key in (
                        "command_id",
                        "kind",
                        "status",
                        "passed",
                        "exit_code",
                        "total",
                        "failed",
                        "skipped",
                        "junit_hash",
                    )
                },
            )
            value = {key: command.get(key) for key in ("error", "failures", "failure_details")}
        text = json.dumps(sanitize(value), ensure_ascii=False)
        return result | {
            "offset": offset,
            "text": text[offset : offset + limit],
            "truncated": offset + limit < len(text),
            "total": len(text),
        }

    def finish(self, response):
        self.reload()
        from dev_orchestra.domain.models import WorkspaceTestResult

        model = WorkspaceTestResult if self.data["role"] == "tester" else WorkspaceResult
        response = model.model_validate(response.model_dump(), strict=True)
        self.guard()
        if response.questions:
            return (
                AgentResult(summary=response.summary, questions=response.questions),
                self.data["current_sha"],
                self.data["commits"],
            )
        if self.data["role"] == "tester":
            self.load_test_result(response)
        files = self.changed_files()
        if files:
            self.checkpoint_commit(
                "host-final",
                self.data["current_sha"],
                {name: self.data["files"][name]["hash"] for name in files},
                describe_commit(response.commit_message, response.summary, files),
                response.summary,
                _host=True,
            )
        patch = self.git.diff(self.data["input_sha"], self.data["current_sha"])
        if not patch and self.data["role"] != "tester" and not response.no_change_reason.strip():
            from dev_orchestra.agents.contracts import ResponseError

            raise ResponseError("workspace no change requires an explicit reason")
        if redact(patch) != patch:
            raise PipelineError("sensitive workspace diff denied")
        changed = set(
            filter(
                None,
                self.git.call(
                    "diff", "--name-only", self.data["input_sha"], self.data["current_sha"]
                ).splitlines(),
            )
        )
        changes = [value for name, value in self.data["changes"].items() if name in changed]
        summary = response.summary
        if not patch:
            summary += "\n무변경 사유: " + response.no_change_reason
        extra = (
            {"command_ids": response.command_ids, "coverage": response.coverage}
            if self.data["role"] == "tester"
            else {}
        )
        result = AgentResult(summary=summary, patch=patch, test_changes=changes, **extra)
        return result, self.data["current_sha"], self.data["commits"]


def load_session(path):
    """Private host launch descriptor; never supplied by model tool arguments."""
    descriptor = json.loads(Path(path).read_text())
    path = Path(descriptor["session"])
    kind = descriptor.get("kind") or json.loads(path.read_text()).get("kind")
    store = Store(Path(descriptor["database"]))
    if kind == "review":
        return ReviewEvidenceSession(path, store)
    return WorkspaceSession(path, store, _run_id=descriptor.get("run_id"))


class ReviewEvidenceSession:
    """Read-only, bounded access to the reviewed candidate's host test artifact."""

    def __init__(self, path, store):
        self.path, self.store = Path(path), store
        self.data = json.loads(self.path.read_text())
        self.git = Git(Path(self.data["worktree"]))

    @classmethod
    def create(cls, store, run, step_id, directory):
        candidates = [
            step
            for step in store.steps(run["id"])
            if step["role"] == "tester"
            and step["status"] == "completed"
            and step["output_sha"] == run["current_sha"]
            and step.get("revision", 0) == run.get("revision", 0)
        ]
        if not candidates:
            raise PipelineError("review requires candidate-bound host test evidence")
        test_step = candidates[-1]
        path = Path(directory) / "review-evidence-session.json"
        write(
            path,
            {
                "kind": "review",
                "role": "reviewer",
                "run_id": run["id"],
                "repo": run["repo"],
                "worktree": run["worktree"],
                "sha": run["current_sha"],
                "revision": run.get("revision", 0),
                "step_id": step_id,
                "evidence_id": "step-" + str(test_step["id"]),
                "test_step": test_step,
            },
        )
        return cls(path, store)

    def read_test_evidence(self, evidence_id, offset=0, limit=8000):
        run = self.store.get(self.data["run_id"])
        step = next(
            (step for step in self.store.steps(run["id"]) if step["id"] == self.data["step_id"]),
            None,
        )
        if (
            run["state"] != "reviewing"
            or run["next_role"] != "reviewer"
            or run["control"]
            or not step
            or step["status"] != "running"
            or run["current_sha"] != self.data["sha"]
            or self.git.sha() != self.data["sha"]
            or run.get("revision", 0) != self.data["revision"]
            or evidence_id != self.data["evidence_id"]
            or offset < 0
            or not 1 <= limit <= 8000
        ):
            raise PipelineError("review evidence range or ownership denied")
        from dev_orchestra.application.results import Results

        value = Results(self.store).payload(run, self.data["test_step"])
        # Raw code patches are deliberately not a diagnostic retrieval payload.
        value = {key: value.get(key) for key in ["sha", "revision", "execution", "test_selection"]}
        text = json.dumps(value, ensure_ascii=False)
        return {
            "evidence_id": evidence_id,
            "offset": offset,
            "text": text[offset : offset + limit],
            "truncated": offset + limit < len(text),
            "total": len(text),
        }
