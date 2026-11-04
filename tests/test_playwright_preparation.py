"""Automatic browser preparation must not execute repository code with network access."""

import base64
import hashlib
import io
import json
import os
import platform
import shutil
import tarfile
from pathlib import Path

import pytest

from dev_orchestra.config import TestCommand as Command
from dev_orchestra.config import Tests as Commands
from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure import playwright


def core(root, version="1.55.1", prefix=""):
    path = root / prefix / "node_modules/playwright-core"
    path.mkdir(parents=True)
    (path / "package.json").write_text(json.dumps({"name": "playwright-core", "version": version}))
    (path / "cli.js").write_text("throw Error('repository code must not run')")
    return path


def archive(name="package/cli.js", kind=tarfile.REGTYPE):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        entry = tarfile.TarInfo(name)
        entry.type = kind
        entry.linkname = "/outside"
        content = b"official installer fixture"
        entry.size = len(content) if kind == tarfile.REGTYPE else 0
        tar.addfile(entry, io.BytesIO(content))
    return output.getvalue()


def mock_registry(monkeypatch, payload, integrity=None):
    checksum = "sha512-" + base64.b64encode(hashlib.sha512(payload).digest()).decode()
    urls = []

    def fetch(url, limit, stop):
        urls.append(url)
        if url.endswith(".tgz"):
            return payload
        return json.dumps(
            {
                "name": "playwright-core",
                "version": "1.55.1",
                "dist": {
                    "integrity": integrity or checksum,
                    "tarball": "https://untrusted.invalid",
                },
            }
        ).encode()

    monkeypatch.setattr(playwright, "fetch", fetch)
    return urls


def test_official_installer_is_integrity_checked_and_does_not_trust_tarball_url(
    tmp_path, monkeypatch
):
    urls = mock_registry(monkeypatch, archive())
    target = tmp_path / "official"
    playwright.installer("1.55.1", target, lambda: None)
    assert (target / "cli.js").read_text() == "official installer fixture"
    assert urls == [
        "https://registry.npmjs.org/playwright-core/1.55.1",
        "https://registry.npmjs.org/playwright-core/-/playwright-core-1.55.1.tgz",
    ]


@pytest.mark.parametrize("kind", ["checksum", "traversal", "symlink"])
def test_rejects_corrupt_or_escaping_installer_before_execution(tmp_path, monkeypatch, kind):
    data = (
        archive("package/../../outside")
        if kind == "traversal"
        else archive(kind=tarfile.SYMTYPE if kind == "symlink" else tarfile.REGTYPE)
    )
    mock_registry(monkeypatch, data, "sha512-invalid" if kind == "checksum" else None)
    with pytest.raises(PipelineError, match="checksum|unsafe"):
        playwright.installer("1.55.1", tmp_path / "official", lambda: None)
    assert not (tmp_path / "outside").exists()


def test_nested_project_uses_its_resolved_core_version(tmp_path):
    root = tmp_path.resolve()
    core(root, "1.60.0")
    test = root / "web/node_modules/@playwright/test"
    test.mkdir(parents=True)
    (test / "package.json").write_text(json.dumps({"name": "@playwright/test"}))
    package = test / "node_modules/playwright"
    package.mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "playwright"}))
    core(package)
    assert playwright.installed_version(root, root / "web") == "1.55.1"


@pytest.mark.parametrize("version", ["../../outside", "1.55.1; command", "latest"])
def test_project_cannot_select_arbitrary_installer_url(tmp_path, version):
    core(tmp_path, version)
    with pytest.raises(PipelineError, match="stable Playwright"):
        playwright.installed_version(tmp_path, tmp_path)


def test_automatic_preparation_reuses_official_tool_and_keeps_policy_and_source(
    tmp_path, monkeypatch
):
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    package = core(root)
    cache = tmp_path / "shared-browsers"
    monkeypatch.setattr(playwright, "browser_cache", lambda: cache)
    monkeypatch.setattr(playwright.platform, "system", lambda: "Darwin")
    monkeypatch.setenv("PLAYWRIGHT_DOWNLOAD_HOST", "https://untrusted.invalid")
    monkeypatch.setenv("NODE_OPTIONS", "--require=untrusted.js")
    calls = []
    urls = mock_registry(monkeypatch, archive())

    def execute(argv, cwd, timeout, log, **kwargs):
        calls.append(argv)
        assert argv[0] == "/usr/bin/sandbox-exec"
        assert str(root) not in argv[2]
        assert argv[-2] == str(cache / ".devorchestra-installers/1.55.1/cli.js")
        assert argv[-1] == "install"
        env = kwargs["env"]
        assert "NODE_OPTIONS" not in env and "PLAYWRIGHT_DOWNLOAD_HOST" not in env
        assert env["PLAYWRIGHT_SKIP_BROWSER_GC"] == "1"
        assert Path(env["HOME"]) == cwd and cwd != root
        return 0, "ready"

    monkeypatch.setattr(playwright, "execute", execute)
    command = Command(
        argv=["npm", "run", "test:browser"],
        junit_report="build/browser.xml",
        env={"PLAYWRIGHT_BROWSERS_PATH": str(cache)},
    )
    second = command.model_copy(deep=True)
    second.junit_report = "build/second.xml"
    cfg = Commands(commands={"browser": command, "same-version": second})
    before = cfg.model_dump()
    for _ in range(2):
        playwright.prepare_browsers(root, cfg, tmp_path / "preflight")
    assert len(calls) == 2 and len(urls) == 2
    assert cfg.model_dump() == before
    assert "repository code" in (package / "cli.js").read_text()


def test_browser_failure_and_cancel_preserve_resumability(tmp_path, monkeypatch):
    root = (tmp_path / "repo").resolve()
    root.mkdir()
    core(root)
    cache = tmp_path / "shared"
    monkeypatch.setattr(playwright, "browser_cache", lambda: cache)
    monkeypatch.setattr(playwright.platform, "system", lambda: "Darwin")
    mock_registry(monkeypatch, archive())
    cfg = Commands(
        commands={
            "browser": Command(
                argv=["npm", "test"],
                junit_report="build/x.xml",
                env={"PLAYWRIGHT_BROWSERS_PATH": str(cache)},
            )
        }
    )
    monkeypatch.setattr(playwright, "execute", lambda *a, **kw: (1, "download failed"))
    with pytest.raises(PipelineError, match="browser.*download failed"):
        playwright.prepare_browsers(root, cfg, tmp_path / "preflight")
    with pytest.raises(StopRequested):
        playwright.prepare_browsers(root, cfg, tmp_path / "preflight", lambda: "pause")


def test_custom_browser_cache_is_not_automatically_modified(tmp_path, monkeypatch):
    cfg = Commands(
        commands={
            "browser": Command(
                argv=["npm", "test"],
                junit_report="build/x.xml",
                env={"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path / "custom")},
            )
        }
    )
    monkeypatch.setattr(playwright, "installer", lambda *a: pytest.fail("custom cache is explicit"))
    playwright.prepare_browsers(tmp_path, cfg, tmp_path / "preflight")
    assert not (tmp_path / "custom").exists()


@pytest.mark.skipif(
    os.environ.get("DEVORCHESTRA_TEST_SANDBOX") != "1"
    or platform.system() != "Darwin"
    or not shutil.which("node"),
    reason="opt-in macOS installer sandbox check",
)
def test_actual_installer_sandbox_denies_unrelated_reads_and_writes(tmp_path):
    root = tmp_path.resolve()
    scratch, cache = root / "scratch", root / "cache"
    scratch.mkdir()
    cache.mkdir()
    secret = root / "private-project.txt"
    secret.write_text("must remain private")
    output = root / "forbidden-write.txt"
    cli = cache / "cli.js"
    cli.write_text("""
const fs = require('fs');
let deniedRead = false, deniedWrite = false;
try { fs.readFileSync(process.argv[3]); } catch (e) { deniedRead = e.code === 'EPERM'; }
try { fs.writeFileSync(process.argv[4], 'unsafe'); } catch (e) { deniedWrite = e.code === 'EPERM'; }
fs.writeFileSync(process.env.HOME + '/allowed.txt', 'ok');
console.log(JSON.stringify({deniedRead, deniedWrite}));
""")
    node = Path(shutil.which("node")).resolve()
    command = playwright.installer_command(node, scratch, cache, cache)
    rc, result = playwright.execute(
        [*command, str(secret), str(output)],
        scratch,
        15,
        root / "sandbox.log",
        env={"PATH": os.environ["PATH"], "HOME": str(scratch), "TMPDIR": str(scratch)},
    )
    assert rc == 0, result
    assert json.loads(result) == {"deniedRead": True, "deniedWrite": True}
    assert not output.exists() and (scratch / "allowed.txt").read_text() == "ok"
