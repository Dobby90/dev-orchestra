"""Preserve verified test reports before ordinary, non-forced worktree removal."""

import shutil
import tempfile
from pathlib import Path

from dev_orchestra.application.results import Results
from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.evidence import checkpoints
from dev_orchestra.infrastructure.files import contained, digest
from dev_orchestra.infrastructure.git import Git
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
    config = Config.model_validate(run["config"]).tests
    tests = checkpoints(store, run).get("tester", {})
    reports = {}
    if tests.get("sha") == run.get("current_sha") and tests.get("revision", 0) == run.get(
        "revision", 0
    ):
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
    unknown = untracked - reports.keys()
    if unknown:
        raise PipelineError(
            "Worktree에 확인되지 않은 파일이 있어 정리를 중단했습니다. 파일을 보존합니다: "
            + ", ".join(sorted(unknown)[:10])
        )
    # Keep original reports as well as retained evidence; never delete unknown output folders.
    archive = Path(tempfile.mkdtemp(prefix="cleanup-reports-", dir=run["directory"]))
    for name in sorted(untracked):
        source = contained(worktree, name)
        if digest(source.read_bytes()) != reports[name]:
            raise PipelineError("JUnit 보고서가 변경되어 정리를 중단했습니다. 파일을 보존합니다.")
        target = contained(archive, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), target)
