"""Preserve verified test outputs before ordinary, non-forced worktree removal."""

import shutil
import tempfile
from pathlib import Path

from dev_orchestra.application.results import Results
from dev_orchestra.application.test_selection import selected_config
from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.evidence import checkpoints
from dev_orchestra.infrastructure.files import contained, digest
from dev_orchestra.infrastructure.git import Git
from dev_orchestra.infrastructure.jest_outputs import cache_path, is_cache_file, output_digest
from dev_orchestra.infrastructure.runtime_adapters import jest_json_path
from dev_orchestra.infrastructure.test_runner import junit_bundle, junit_paths


def preserve_test_reports(store, run):
    worktree = Path(run["worktree"])
    git = Git(worktree)
    untracked = set(
        filter(None, git.call("ls-files", "--others", "--exclude-standard", "-z").split("\0"))
    )
    if not untracked:
        return
    if git.call("diff", "--name-only", "HEAD", "--"):
        raise PipelineError("Worktree에 미커밋 변경이 있어 정리를 중단했습니다. 파일을 보존합니다.")
    tests = checkpoints(store, run).get("tester", {})
    reports = {}
    if tests.get("sha") == run.get("current_sha") and tests.get("revision", 0) == run.get(
        "revision", 0
    ):
        selection = tests.get("test_selection")
        config = (
            selected_config(run, selection)
            if selection
            else Config.model_validate(run["config"]).tests
        )
        outputs = [contained(worktree, p) for p in config.output_paths]
        for result in tests.get("execution", {}).get("commands", []):
            command = config.commands.get(result.get("command_id"))
            if not command or not command.junit_report:
                continue
            paths = junit_paths(worktree, command.junit_report, outputs)
            if not paths:
                continue
            if result.get("junit_artifact_id"):
                retained, _ = Results(store).artifact(run["id"], result["junit_artifact_id"])
            elif result.get("junit_path"):
                directory = Path(run["directory"])
                retained_path = Path(result["junit_path"])
                if not retained_path.is_relative_to(directory):
                    raise PipelineError("JUnit 근거 경로가 Run 기록 폴더 밖에 있습니다.")
                retained = contained(directory, retained_path.relative_to(directory).as_posix())
            else:
                continue
            if digest(retained.read_bytes()) != result.get("junit_hash"):
                raise PipelineError("저장된 JUnit 근거가 변경되어 정리를 중단했습니다.")
            if junit_bundle(paths) != retained.read_text():
                continue
            for path in paths:
                reports[path.relative_to(worktree).as_posix()] = digest(path.read_bytes())
            if result.get("jest_outputs") and (json_report := jest_json_path(worktree, command)):
                cache = cache_path(worktree, command, outputs)
                for name in untracked.intersection(result["jest_outputs"]):
                    path = contained(worktree, name)
                    if path != json_report and not is_cache_file(path, cache):
                        continue
                    if not any(path.is_relative_to(output) for output in outputs):
                        continue
                    sha = result["jest_outputs"][name]
                    if output_digest(path) == sha:
                        reports[name] = sha
    unknown = untracked - reports.keys()
    if unknown:
        raise PipelineError(
            "Worktree에 확인되지 않은 파일이 있어 정리를 중단했습니다. 파일을 보존합니다: "
            + ", ".join(sorted(unknown)[:10])
        )
    # Preserve the original outputs outside the Worktree before non-forced Git removal.
    archive = Path(tempfile.mkdtemp(prefix="cleanup-reports-", dir=run["directory"]))
    for name in sorted(untracked):
        source = contained(worktree, name)
        if output_digest(source) != reports[name]:
            raise PipelineError("검증 산출물이 변경되어 정리를 중단했습니다. 파일을 보존합니다.")
        target = contained(archive, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), target)
