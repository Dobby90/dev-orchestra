import os
import sys

import pytest

from dev_orchestra.config import Config
from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure.files import contained
from dev_orchestra.infrastructure.locking import OwnershipError, lock
from dev_orchestra.infrastructure.process import execute
from dev_orchestra.infrastructure.test_runner import sandbox_command


def test_root_alias_is_allowed(tmp_path):
    alias = tmp_path / "alias"
    root = tmp_path / "root"
    root.mkdir()
    alias.symlink_to(root, target_is_directory=True)
    assert contained(alias, "file") == root / "file"


def test_test_command_id_cannot_traverse():
    with pytest.raises(ValueError):
        Config.model_validate(
            {
                "tests": {
                    "commands": {"../evil": {"argv": ["pytest"], "junit_report": "build/out.xml"}}
                }
            }
        )


def test_execution_lock_is_exclusive(tmp_path):
    with lock(tmp_path / "lock"):
        with pytest.raises(OwnershipError):
            with lock(tmp_path / "lock"):
                pass


def test_process_cancel(tmp_path):
    with pytest.raises(StopRequested):
        execute(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            tmp_path,
            10,
            tmp_path / "log",
            stop=lambda: "cancel",
        )


def test_output_limit(tmp_path):
    with pytest.raises(PipelineError, match="limit"):
        execute(
            [sys.executable, "-c", "print('x' * (9 * 1024 * 1024))"], tmp_path, 10, tmp_path / "log"
        )


@pytest.mark.skipif(
    os.getenv("DEVORCHESTRA_TEST_SANDBOX") != "1", reason="opt-in actual OS sandbox test"
)
def test_actual_sandbox_denies_source_writes_and_network(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    file = source / "main.py"
    file.write_text("protected")
    output = tmp_path / "output"
    output.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    script = """
from pathlib import Path
import socket
try:
    Path('main.py').write_text('bad')
except PermissionError:
    pass
else:
    raise AssertionError('source writable')
try:
    socket.create_connection(('1.1.1.1', 443), timeout=1)
except PermissionError:
    pass
else:
    raise AssertionError('network allowed')
"""
    argv = sandbox_command(source, [output], scratch, [sys.executable, "-c", script])
    rc, text = execute(argv, source, 10, tmp_path / "sandbox.log")
    assert rc == 0, text
    assert file.read_text() == "protected"
