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
