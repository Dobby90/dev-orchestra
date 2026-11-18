"""Container toolchains keep commands frozen, source private and cleanup authenticated."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_workspace import edit, start_session
from test_workspace import setup as setup

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure import containers
from dev_orchestra.infrastructure.containers import (
    cleanup_owned_container,
    run_arguments,
    specification,
)


def prepared():
    return {
        "image_id": "sha256:" + "a" * 64,
        "configuration_hash": "hash",
        "specification": {"node": "24.12.0", "pnpm": "11.25.0"},
        "docker": ["/host/docker", "--host", "unix:///local/docker.sock"],
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"image": "postgres:latest"},
        {"image": "private.invalid/image:1"},
        {"node": "24"},
        {"packages": ["curl;touch source"]},
        {"privileged": True},
    ],
)
def test_environment_rejects_unbounded_or_executable_configuration(tmp_path, extra):
    (tmp_path / containers.MANIFEST).write_text(
        json.dumps({"image": "postgres:17-bookworm", **extra})
    )
    with pytest.raises(PipelineError):
        specification(tmp_path)


def test_test_container_has_no_network_or_socket_and_only_declared_writable_outputs(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / ".env").write_text("PASSWORD=private-value")
    (source / ".env.example").write_text("HOST=127.0.0.1\n")
    output = source / "build"
    output.mkdir()
    directory = tmp_path / "evidence"
    directory.mkdir()
    bootstrap = Path(containers.__file__).with_name("unittest_bootstrap.py")
    argv, env = run_arguments(
        source, source, [output], tmp_path, [sys.executable, str(bootstrap)], prepared(), directory
    )
    assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv and "--cap-drop=ALL" in argv
    assert "--privileged" not in argv and "--publish" not in argv
    assert f"type=bind,source={source},target={source},readonly" in argv
    assert f"type=bind,source={output},target={output}" in argv
    assert f"type=bind,source=/dev/null,target={source / '.env'},readonly" in argv
    assert not any(str(source / ".env.example") in value for value in argv)
    assert argv[-2:] == ["python3", "/opt/devorchestra/unittest_bootstrap.py"]
    owner = json.loads(Path(env["DEVORCHESTRA_CONTAINER_OWNER"]).read_text())
    assert owner["image_id"] == prepared()["image_id"] and owner["token"]


def test_cleanup_never_removes_a_container_with_different_ownership(tmp_path, monkeypatch):
    path = tmp_path / "owner.json"
    path.write_text(
        json.dumps(
            {
                "name": "devorchestra-" + "b" * 32,
                "token": "expected",
                "docker": ["docker"],
                "image_id": "sha256:expected",
            }
        )
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "Config": {"Labels": {"devorchestra.owner": "someone-else"}},
                    "Image": "sha256:expected",
                }
            ),
            "",
        )

    monkeypatch.setattr(containers.subprocess, "run", run)
    with pytest.raises(PipelineError, match="ownership mismatch"):
        cleanup_owned_container(path)
    assert len(calls) == 1


def test_missing_container_is_not_confirmed_when_daemon_is_unavailable(tmp_path, monkeypatch):
    path = tmp_path / "owner.json"
    path.write_text(
        json.dumps(
            {
                "name": "devorchestra-" + "b" * 32,
                "token": "expected",
                "docker": ["docker"],
                "image_id": "sha256:expected",
            }
        )
    )
    monkeypatch.setattr(
        containers.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "connection refused"),
    )
    with pytest.raises(PipelineError, match="cannot verify"):
        cleanup_owned_container(path)


def test_developer_preparation_records_generated_lockfiles_through_editor(setup, monkeypatch):
    session, _ = start_session(setup)
    edit(session, containers.MANIFEST, '{"image":"postgres:17-bookworm"}')
    lock = '{"lockfileVersion":3,"packages":{}}\n'
    monkeypatch.setattr(
        containers,
        "prepare_dependencies",
        lambda *args, **kwargs: prepared() | {"generated_lockfiles": {"package-lock.json": lock}},
    )
    result = session.prepare_environment()
    assert (session.root / "package-lock.json").read_text() == lock
    assert result["generated_lockfiles"][0]["file"] == "package-lock.json"
    assert not session.data["testing"] and "package-lock.json" in session.data["files"]
    session.guard()


def test_tester_cannot_install_or_generate_product_lockfiles(setup):
    session, _ = start_session(setup, role="tester")
    with pytest.raises(PipelineError, match="restricted"):
        session.prepare_environment()


def test_preparation_failure_releases_edit_lock_but_preserves_source(setup, monkeypatch):
    session, _ = start_session(setup)
    edit(session, containers.MANIFEST, '{"image":"postgres:17-bookworm"}')

    def fail(*args, **kwargs):
        raise PipelineError("image unavailable")

    monkeypatch.setattr(containers, "prepare_dependencies", fail)
    with pytest.raises(PipelineError, match="image unavailable"):
        session.prepare_environment()
    assert session.data["testing"] is False and not (session.root / "package-lock.json").exists()


@pytest.mark.parametrize("hooked", [False, True])
def test_online_installer_receives_only_sanitized_manifests(tmp_path, monkeypatch, hooked):
    from dev_orchestra.config import TestCommand as Command
    from dev_orchestra.config import Tests as Commands

    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "fixture",
                "version": "1.0.0",
                "dependencies": {"is-number": "7.0.0"},
                "scripts": {"postinstall": "touch SOURCE"},
            }
        )
    )
    (root / "SOURCE").write_text("private source")
    (root / ".npmrc").write_text("private config")
    (root / "build").mkdir()
    (root / "build/package.json").write_text('{"name":"cached-output"}')
    if hooked:
        package = json.loads((root / "package.json").read_text())
        package["pnpm"] = {"configDependencies": {"untrusted": "1.0.0"}}
        (root / "package.json").write_text(json.dumps(package))
    directory = tmp_path / "evidence"
    directory.mkdir()
    monkeypatch.setattr(containers, "toolchain", lambda *args, **kwargs: prepared())

    def install(base, argv, directory, *args, **kwargs):
        mount = next(
            value
            for value in argv
            if value.startswith("type=bind,source=") and str(directory) in value
        )
        view = Path(mount.split("source=", 1)[1].split(",target=", 1)[0])
        assert not (view / "SOURCE").exists() and not (view / ".npmrc").exists()
        assert not (view / "build/package.json").exists()
        value = json.loads((view / "package.json").read_text())
        assert "scripts" not in value
        assert "--ignore-scripts" in argv
        (view / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{}}')
        return "ok"

    monkeypatch.setattr(containers, "command", install)
    config = Commands(
        commands={"unit": Command(argv=["node", "test.js"], junit_report="build/junit.xml")}
    )
    if hooked:
        with pytest.raises(PipelineError, match="unsupported package manager hooks"):
            containers.prepare_dependencies(root, config, directory, generate_locks=True)
        return
    value = containers.prepare_dependencies(root, config, directory, generate_locks=True)
    assert "package-lock.json" in value["generated_lockfiles"]
    assert (
        not (root / "package-lock.json").exists()
        and (root / "SOURCE").read_text() == "private source"
    )


def test_mount_path_cannot_inject_docker_mount_options(tmp_path):
    source = tmp_path / "source,target=/outside"
    source.mkdir(parents=True)
    with pytest.raises(PipelineError, match="mount path"):
        run_arguments(source, source, [], tmp_path, ["python3"], prepared(), tmp_path)


def test_already_removed_container_still_requires_reachable_daemon(tmp_path, monkeypatch):
    path = tmp_path / "owner.json"
    path.write_text(
        json.dumps(
            {
                "name": "devorchestra-" + "b" * 32,
                "token": "expected",
                "docker": ["docker"],
                "image_id": "sha256:expected",
            }
        )
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 1 if "inspect" in argv else 0, "", "error: no such object: container"
        )

    monkeypatch.setattr(containers.subprocess, "run", run)
    cleanup_owned_container(path)
    assert len(calls) == 2 and "version" in calls[-1]


def test_cleanup_removes_only_authenticated_container_id(tmp_path, monkeypatch):
    path = tmp_path / "owner.json"
    path.write_text(
        json.dumps(
            {
                "name": "devorchestra-" + "b" * 32,
                "token": "expected",
                "docker": ["docker"],
                "image_id": "sha256:expected",
            }
        )
    )
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                {
                    "Id": "owned-id",
                    "Image": "sha256:expected",
                    "Config": {"Labels": {"devorchestra.owner": "expected"}},
                }
            ),
            "",
        )

    monkeypatch.setattr(containers.subprocess, "run", run)
    cleanup_owned_container(path)
    assert calls[-1] == ["docker", "rm", "--force", "owned-id"]


def test_host_tools_reports_existing_node_and_pnpm_versions_without_inventing_pins(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        containers.shutil,
        "which",
        lambda name: "/host/" + name if name in {"node", "pnpm"} else None,
    )
    monkeypatch.setattr(
        containers,
        "execute",
        lambda argv, *args, **kwargs: (
            0,
            "v24.12.0\n" if argv[0].endswith("node") else "11.25.0\n",
        ),
    )
    values = containers.host_tools(tmp_path)
    assert values["node"] == {"available": True, "version": "24.12.0"}
    assert values["pnpm"] == {"available": True, "version": "11.25.0"}
    assert values["postgres"]["available"] is False


@pytest.mark.skipif(
    os.getenv("DEVORCHESTRA_TEST_DOCKER") != "1", reason="opt-in Docker toolchain smoke"
)
def test_actual_container_runs_frozen_bootstrap_with_node_and_postgresql():
    import tempfile

    from dev_orchestra.config import TestCommand as Command
    from dev_orchestra.config import Tests as Commands
    from dev_orchestra.infrastructure.test_runner import TestRunner

    data = Path.cwd() / "data"
    data.mkdir(exist_ok=True)
    with (
        tempfile.TemporaryDirectory(prefix="container-smoke-", dir=data) as temporary,
        tempfile.TemporaryDirectory(prefix="container-evidence-", dir=data) as evidence,
    ):
        root = Path(temporary).resolve()
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        (root / containers.MANIFEST).write_text(
            json.dumps(
                {
                    "image": "postgres:17-bookworm",
                    "node": "24.12.0",
                    "pnpm": "11.25.0",
                    "packages": ["redis-server"],
                }
            )
        )
        (root / "tests").mkdir()
        (root / "package.json").write_text(
            json.dumps(
                {
                    "name": "container-fixture",
                    "version": "1.0.0",
                    "packageManager": "pnpm@11.25.0",
                    "dependencies": {"is-number": "7.0.0"},
                    "scripts": {"postinstall": "touch tests/FORBIDDEN"},
                }
            )
        )
        (root / ".gitignore").write_text("node_modules/\nbuild/\n")
        (root / "tests/test_tools.py").write_text("""import errno, socket, subprocess, unittest
from pathlib import Path
class Tools(unittest.TestCase):
 def test_actual_tools_and_isolation(self):
  def run(args):
   return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT).strip()
  self.assertEqual(run(['node','--version']), 'v24.12.0')
  self.assertEqual(run(['pnpm','--version']), '11.25.0')
  self.assertEqual(run(['node','-p',"require('is-number')(42)"]), 'true')
  self.assertFalse(Path('tests/FORBIDDEN').exists())
  self.assertIn('17.', run(['pg_dump','--version']))
  with self.assertRaises(OSError) as denied: Path('tests/forbidden.py').write_text('bad')
  self.assertIn(denied.exception.errno, [errno.EROFS, errno.EACCES, errno.EPERM])
  with self.assertRaises(OSError): socket.create_connection(('1.1.1.1',443),timeout=0.2)
  run(['initdb','-D','build/pg','-A','trust','-U','postgres','--no-locale'])
  try:
   run(['pg_ctl','-D','build/pg','-l','build/pg.log','-o',"-k /tmp -c listen_addresses=''",'-w','start'])
   self.assertEqual(run(['psql','-h','/tmp','-U','postgres','-Atc','select 42']), '42')
   self.assertIn('PostgreSQL database dump',run(['pg_dump','-h','/tmp','-U','postgres','--schema-only']))
  finally: run(['pg_ctl','-D','build/pg','-m','immediate','-w','stop'])
  run(['redis-server','--port','0','--unixsocket','/tmp/redis-fixture.sock','--daemonize','yes','--save','','--appendonly','no','--dir','build','--pidfile','build/redis.pid'])
  try:
   self.assertEqual(run(['redis-cli','-s','/tmp/redis-fixture.sock','SET','fixture','verified']), 'OK')
   self.assertEqual(run(['redis-cli','-s','/tmp/redis-fixture.sock','GET','fixture']), 'verified')
  finally: run(['redis-cli','-s','/tmp/redis-fixture.sock','SHUTDOWN','NOSAVE'])
""")
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "컨테이너 검증 fixture",
            ],
            check=True,
        )
        bootstrap = Path(containers.__file__).with_name("unittest_bootstrap.py")
        config = Commands(
            commands={
                "bootstrap": Command(
                    argv=[sys.executable, str(bootstrap)], junit_report="build/junit.xml"
                )
            },
            output_paths=["build"],
        )
        try:
            value = containers.prepare_dependencies(
                root, config, Path(evidence), generate_locks=True
            )
        except PipelineError as error:
            details = {
                p.name: p.read_text()[-2500:]
                for p in Path(evidence).glob("*")
                if p.name.endswith((".process.json", "dependencies.log"))
            }
            pytest.fail(str(error) + "\n" + json.dumps(details))
        for name, content in value["generated_lockfiles"].items():
            (root / name).write_text(content)
        result = TestRunner().run(root, config, Path(evidence))
        assert result["passed"], json.dumps(result, ensure_ascii=False)
        assert result["commands"][0]["total"] == 1


@pytest.mark.skipif(
    os.getenv("DEVORCHESTRA_TEST_DOCKER") != "1", reason="opt-in Docker timeout cleanup"
)
def test_actual_container_timeout_confirms_owned_container_removal():
    import tempfile

    from dev_orchestra.config import TestCommand as Command
    from dev_orchestra.config import Tests as Commands
    from dev_orchestra.infrastructure.test_runner import TestRunner

    data = Path.cwd() / "data"
    data.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="container-cancel-", dir=data) as task:
        root = Path(task).resolve()
        source, evidence = root / "source", root / "evidence"
        source.mkdir()
        evidence.mkdir()
        subprocess.run(["git", "init", "-q", str(source)], check=True)
        (source / containers.MANIFEST).write_text(
            json.dumps(
                {
                    "image": "postgres:17-bookworm",
                    "node": "24.12.0",
                    "pnpm": "11.25.0",
                    "packages": ["redis-server"],
                }
            )
        )
        config = Commands(
            commands={
                "timeout": Command(
                    argv=["python3", "-c", "import time; time.sleep(60)"],
                    timeout_seconds=1,
                    junit_report="build/timeout.xml",
                )
            },
            output_paths=["build"],
        )
        result = TestRunner().run(source, config, evidence)
        assert not result["passed"] and result["status"] == "timeout", result
        owners = list(evidence.glob("*.owner.json"))
        assert len(owners) == 1
        cleanup_owned_container(owners[0])
        record = json.loads((evidence / "timeout.process.json").read_text())
        assert record["termination_confirmed"] is True
