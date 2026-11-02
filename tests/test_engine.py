import sys

import pytest

from dev_orchestra.config import Config, load_config
from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import relative_path
from dev_orchestra.infrastructure.process import execute, redact
from dev_orchestra.infrastructure.store import Store


def test_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("storage:\n  database: local.db\n")
    assert load_config(path).storage.database == tmp_path / "local.db"
    with pytest.raises(ValueError):
        Config.model_validate({"git": {"auto_push": True}})
    with pytest.raises(ValueError):
        Config.model_validate({"workflow": {"max_fix_attempts": -1}})


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", ".git/config", "tests/../../x", "."])
def test_unsafe_paths(path):
    with pytest.raises(PipelineError):
        relative_path(path)


def test_store(tmp_path):
    store = Store(tmp_path / "state.db")
    store.create({"id": "r"})
    store.update("r", "planning", sha="abc")
    assert store.get("r")["sha"] == "abc"
    assert len(store.events("r")) == 1
    with pytest.raises(PipelineError):
        store.approve("r", "abc", "evidence")


def test_process(tmp_path):
    rc, out = execute([sys.executable, "-c", "print('hello')"], tmp_path, 5, tmp_path / "ok.log")
    assert rc == 0 and "hello" in out
    with pytest.raises(PipelineError, match="timeout"):
        execute(
            [sys.executable, "-c", "import time; time.sleep(10)"],
            tmp_path,
            0.1,
            tmp_path / "timeout.log",
        )
    assert "sk-abc123" not in redact("key sk-abc123 token=foo")


def test_commit_excludes_outputs_when_also_gitignored(tmp_path):
    import subprocess

    from dev_orchestra.infrastructure.git import Git

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git = Git(repo, ["build", ".pytest_cache"])
    git.call("config", "user.name", "검증")
    git.call("config", "user.email", "test@example.invalid")
    (repo / ".gitignore").write_text("build/\n.pytest_cache/\n")
    (repo / "literal[1].py").write_text("original\n")
    for name in ["build", ".pytest_cache"]:
        (repo / name).mkdir()
        (repo / name / "result").write_text("must never be committed")
    sha = git.commit("출력 제외 검증")
    assert sha == git.sha()
    assert git.call("ls-files").splitlines() == [".gitignore", "literal[1].py"]
    assert git.dirty() == ""
    (repo / "literal[1].py").write_text("changed\n")
    assert git.commit("리터럴 경로 수정") != sha
