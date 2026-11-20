"""Public defaults are editable/readable while real environment secrets stay denied."""

import json
import os
import platform
import sys

import pytest
from test_workspace import checkpoint, edit, start_session
from test_workspace import setup as setup

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.process import execute
from dev_orchestra.infrastructure.secret_files import public_environment_example
from dev_orchestra.infrastructure.test_runner import sandbox_command

PUBLIC = "# Public local defaults\nHOST=127.0.0.1\nPORT=3001\nAPI_KEY=\n"


@pytest.mark.parametrize(
    "path,content",
    [
        (".env", PUBLIC),
        (".env.local", PUBLIC),
        (".env.production.example", PUBLIC),
        (".env/private/.env.example", PUBLIC),
        (".env.example", "API_KEY=private-value\n"),
        (".env.example", "DATABASE_URL=postgresql://user:private@127.0.0.1/db\n"),
        (".env.example", "PORT=$(touch source)\n"),
        (".env.example", "source .env\n"),
    ],
)
def test_only_public_examples_are_accepted(path, content):
    assert not public_environment_example(path, content)


def test_public_example_can_be_edited_and_checkpointed_with_source_protection(setup):
    session, _ = start_session(setup)
    edit(session, ".env.example", PUBLIC)
    result = checkpoint(session, "public-example", [".env.example"])
    assert session.git.call("show", result["sha"] + ":.env.example") == PUBLIC.strip()
    with pytest.raises(PipelineError, match="secret file edit denied"):
        edit(session, ".env.example", "API_KEY=private-value\n")
    assert (session.root / ".env.example").read_text() == PUBLIC


@pytest.mark.skipif(platform.system() != "Darwin", reason="macOS sandbox profile")
def test_sandbox_exempts_safe_example_but_denies_actual_and_mislabeled_secrets(tmp_path):
    (tmp_path / ".env.example").write_text(PUBLIC)
    (tmp_path / ".env").write_text("PASSWORD=private-value\n")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / ".env.example").write_text("API_KEY=private-value\n")
    profile = sandbox_command(tmp_path, [], tmp_path, [sys.executable])[2]
    for path in (tmp_path / ".env", nested / ".env.example"):
        assert "(deny file-read* (subpath " + json.dumps(str(path.resolve())) + "))" in profile
    assert (
        "(deny file-read* (subpath " + json.dumps(str((tmp_path / ".env.example").resolve())) + "))"
        not in profile
    )


def test_actual_environment_contents_are_not_read_to_build_an_example_exception(
    tmp_path, monkeypatch
):
    from pathlib import Path

    from dev_orchestra.infrastructure.secret_files import readable_environment_example

    private = tmp_path / ".env"
    private.write_text("PASSWORD=private-value\n")

    def forbidden(*args, **kwargs):
        pytest.fail("real environment content must not be inspected")

    monkeypatch.setattr(Path, "read_text", forbidden)
    assert readable_environment_example(private, ".env") is False


@pytest.mark.skipif(
    platform.system() != "Darwin" or os.getenv("DEVORCHESTRA_TEST_SANDBOX") != "1",
    reason="opt-in macOS sandbox read policy",
)
def test_actual_sandbox_reads_public_example_and_denies_real_environment(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".env.example").write_text(PUBLIC)
    (source / ".env").write_text("PASSWORD=private-value\n")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    script = """
from pathlib import Path
assert 'HOST=127.0.0.1' in Path('.env.example').read_text()
try:
    Path('.env').read_text()
except PermissionError:
    pass
else:
    raise AssertionError('real environment was readable')
try:
    Path('.env.example').write_text('changed')
except PermissionError:
    pass
else:
    raise AssertionError('source was writable')
"""
    argv = sandbox_command(source, [], scratch, [sys.executable, "-c", script])
    rc, output = execute(argv, source, 10, tmp_path / "example-read.log")
    assert rc == 0, output
