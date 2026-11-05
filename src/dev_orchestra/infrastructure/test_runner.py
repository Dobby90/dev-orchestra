"""Offline test execution with read-only source and explicit output directories."""

import platform
import shutil
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path

from dev_orchestra.domain.models import PipelineError, StopRequested
from dev_orchestra.infrastructure.files import contained, digest, write
from dev_orchestra.infrastructure.process import (
    environment,
    execute,
    execution_directory,
    execution_scope,
    sanitize,
)


def sandbox_command(worktree, outputs, scratch, argv, network="none", *, browser=False):
    if platform.system() == "Darwin":
        # Seatbelt applies to every descendant, including repository test/build scripts.
        import json

        def sub(p):
            return "(subpath " + json.dumps(str(p.resolve())) + ")"

        profile = "(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)(allow file-read*)"
        # Test servers and browser subprocesses must be able to stop their own
        # descendants. Other processes remain outside this signal permission.
        profile += "(allow signal (target self) (target children))"
        for name in [
            ".ssh",
            ".aws",
            ".codex",
            ".config/gh",
            ".git-credentials",
            ".m2/settings.xml",
            ".m2/settings-security.xml",
            ".gradle/gradle.properties",
        ]:
            profile += "(deny file-read* " + sub(Path.home() / name) + ")"
        profile += (
            "(allow file-write* "
            + " ".join(sub(p) for p in [scratch, *outputs])
            + ' (literal "/dev/null"))'
        )
        if network == "loopback":
            if browser:
                # Chromium's macOS power monitor needs this specific notification client.
                # Keep all other IOKit services/user clients denied by default.
                profile += '(allow iokit-open (iokit-user-client-class "RootDomainUserClient"))'
            # Chromium registers a per-process rendezvous service for child IPC.
            profile += '(allow mach-register (global-name-regex #"^org[.]chromium[.]Chromium[.]MachPortRendezvousServer[.][0-9]+$"))'
            profile += '(allow mach-register (global-name-regex #"^com[.]google[.]chrome[.]for[.]testing[.]MachPortRendezvousServer[.][0-9]+$"))'
            # JVM tools bind ephemeral wildcard sockets for local file-lock IPC.
            # Traffic remains restricted to loopback in both directions.
            profile += '(allow network-bind (local ip "*:*"))'
            profile += '(allow network-inbound (local ip "localhost:*"))'
            profile += '(allow network-outbound (remote ip "localhost:*"))'
        for item in worktree.rglob("*"):
            if item.name.startswith(".env") or item.suffix in {".pem", ".key"}:
                profile += "(deny file-read* " + sub(item) + ")"
        return ["/usr/bin/sandbox-exec", "-p", profile, *argv]
    if platform.system() == "Linux" and shutil.which("bwrap"):
        if network == "loopback":
            raise PipelineError(
                "loopback browser tests require a supported loopback sandbox (macOS)"
            )
        command = [
            "bwrap",
            "--die-with-parent",
            "--unshare-net",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
        ]
        for p in [scratch, *outputs]:
            command += ["--bind", str(p), str(p)]
        return command + ["--chdir", str(worktree), "--", *argv]
    raise PipelineError("supported OS sandbox unavailable; refusing unsandboxed tests")


def parse_junit(path):
    try:
        if path.stat().st_size > 8 * 1048576:
            raise ValueError("JUnit exceeds safety limit")
        content = path.read_text()
        if "<!DOCTYPE" in content or "<!ENTITY" in content:
            raise ValueError("XML declarations denied")
        root = ET.fromstring(content)
        cases = list(root.iter("testcase"))
        if not cases:
            raise ValueError("no tests executed")
        failures = [
            c.attrib.get("name", "unknown")
            for c in cases
            if c.find("failure") is not None or c.find("error") is not None
        ]
        # Global setup/tool errors may exist only on suite attributes.
        for suite in root.iter():
            if suite.tag in {"testsuite", "testsuites"}:
                declared = int(suite.attrib.get("errors", 0)) + int(suite.attrib.get("failures", 0))
                if declared > len(failures):
                    raise ValueError("suite failure counts disagree with testcase evidence")
        skipped = sum(c.find("skipped") is not None for c in cases)
        if skipped == len(cases):
            raise ValueError("all tests skipped")
        return {
            "total": len(cases),
            "failed": len(failures),
            "skipped": skipped,
            "failures": failures,
            "failure_details": [
                {
                    "name": c.attrib.get("name", "unknown"),
                    "classname": c.attrib.get("classname"),
                    "file": c.attrib.get("file"),
                    "line": c.attrib.get("line"),
                    "message": sanitize(
                        (
                            c.find("failure") if c.find("failure") is not None else c.find("error")
                        ).attrib.get("message", "")
                    )[:16000],
                    "details": sanitize(
                        (
                            c.find("failure") if c.find("failure") is not None else c.find("error")
                        ).text
                        or ""
                    )[:16000],
                }
                for c in cases
                if c.find("failure") is not None or c.find("error") is not None
            ],
        }
    except (OSError, ET.ParseError, ValueError) as error:
        raise PipelineError(f"invalid or absent JUnit test evidence: {error}") from error


def junit_paths(worktree, pattern, outputs):
    """Resolve bounded report globs without following workspace-escaping symlinks."""
    paths = []
    for path in worktree.glob(pattern):
        safe = contained(worktree, path.relative_to(worktree).as_posix())
        if not any(safe.is_relative_to(output) for output in outputs):
            raise PipelineError("JUnit report must be inside a configured output path")
        if not safe.is_file():
            raise PipelineError("JUnit report must be a regular file")
        paths.append(safe)
        if len(paths) > 256:
            raise PipelineError("JUnit report count exceeds safety limit")
    return sorted(paths)


class TestRunner:
    __test__ = False

    def prepare(self, worktree, config, directory, stop=lambda: None):
        from dev_orchestra.infrastructure.dependencies import prepare_dependencies

        prepare_dependencies(worktree, config, directory, stop)

    def check_policy(self, config):
        if platform.system() != "Darwin" and any(
            c.network == "loopback" for c in config.commands.values()
        ):
            raise PipelineError(
                "loopback validation currently requires macOS; update project validation settings"
            )

    def check(self, worktree, stop=lambda: None):
        with tempfile.TemporaryDirectory(prefix="devorchestra-probe-") as temp:
            scratch = Path(temp).resolve()
            argv = sandbox_command(worktree, [], scratch, ["/usr/bin/true"])
            rc, _ = execute(
                argv,
                worktree,
                10,
                (
                    execution_directory() / "preflight" / (uuid.uuid4().hex + "-sandbox.log")
                    if execution_directory()
                    else scratch / "probe.log"
                ),
                stop=stop,
            )
            if rc:
                raise PipelineError("OS sandbox unavailable; refusing model calls and tests")

    def run(self, worktree, config, directory, stop=lambda: None):
        if not config.commands:
            raise PipelineError("configure at least one allowlisted test command")
        results = []
        directory.mkdir(parents=True, exist_ok=True)
        for command_id, command in config.commands.items():
            cwd = (
                worktree
                if command.working_directory == "."
                else contained(worktree, command.working_directory)
            )
            outputs = [contained(worktree, p) for p in config.output_paths]
            if any(p.is_file() for p in outputs):
                raise PipelineError("output path must be a directory")
            # Never grant write access to any tracked production/test file via output paths.
            from dev_orchestra.infrastructure.git import Git

            tracked = [name for name in Git(worktree).call("ls-files", "-z").split("\0") if name]
            if any((worktree / name).is_relative_to(p) for name in tracked for p in outputs):
                raise PipelineError("output path contains tracked source files")
            for p in outputs:
                p.mkdir(parents=True, exist_ok=True)
            from dev_orchestra.infrastructure.runtime_adapters import (
                convert_jest_report,
                jest_json_path,
                runtime_command,
            )

            jest_json = jest_json_path(worktree, command)
            if jest_json:
                if not any(jest_json.is_relative_to(p) for p in outputs):
                    raise PipelineError("Jest JSON report must be inside an allowed output")
                jest_json.unlink(missing_ok=True)
            if command.junit_report:
                report_pattern = contained(worktree, command.junit_report)
                if not any(report_pattern.is_relative_to(p) for p in outputs) and not any(
                    token in command.junit_report for token in ("*", "?", "[")
                ):
                    raise PipelineError("JUnit report must be inside a configured output path")
                # Remove every previous report, including Java's per-class XML files.
                for report in junit_paths(worktree, command.junit_report, outputs):
                    report.unlink()
            started = time.monotonic()
            result = {
                "command_id": command_id,
                "kind": command.kind,
                "working_directory": command.working_directory,
                "argv": command.argv,
                "exit_code": None,
                "status": "unavailable",
                "total": None,
                "failed": None,
                "skipped": None,
                "failures": [],
                "failure_details": [],
                "passed": False,
                "junit_path": None,
                "junit_hash": None,
                "error": None,
            }
            diagnostic = ""
            try:
                from dev_orchestra.infrastructure.coordination import host_root, slot

                resource = (
                    slot(
                        host_root() / "loopback", execution_directory() or directory, stop, limit=1
                    )
                    if command.network == "loopback"
                    else nullcontext()
                )
                with (
                    resource,
                    execution_scope(execution_directory() or directory),
                    tempfile.TemporaryDirectory(prefix="devorchestra-test-") as temp,
                ):
                    scratch = Path(temp).resolve()
                    env = environment() | {
                        "HOME": str(scratch),
                        "TMPDIR": str(scratch),
                        "TMP": str(scratch),
                        "TEMP": str(scratch),
                        # Chromium on macOS ignores TMPDIR for some temporary files.
                        "MAC_CHROMIUM_TMPDIR": str(scratch),
                        "JAVA_TOOL_OPTIONS": f'-Duser.home="{scratch}" -Djava.io.tmpdir="{scratch}"',
                    }
                    env.update(command.env)
                    from dev_orchestra.infrastructure.java import java_environment

                    env.update(java_environment(worktree, command))
                    runtime_argv, adapters = runtime_command(worktree, command, directory, env)
                    result["runtime_argv"] = runtime_argv
                    result["runtime_adapters"] = adapters
                    options = {"browser": True} if "PLAYWRIGHT_BROWSERS_PATH" in command.env else {}
                    argv = sandbox_command(
                        worktree, outputs, scratch, runtime_argv, command.network, **options
                    )
                    rc, output = execute(
                        argv,
                        cwd,
                        command.timeout_seconds,
                        directory / f"{command_id}.log",
                        stop=stop,
                        env=env,
                    )
                result["exit_code"] = rc
                if rc:
                    diagnostic = f"exit code {rc}: {sanitize(output[-1200:].strip())}"
                    result["error"] = diagnostic
                if command.kind == "check":
                    result.update(passed=rc == 0, status="passed" if rc == 0 else "failed")
                else:
                    result["status"] = "parse_error"
                    if jest_json:
                        target = contained(worktree, command.junit_report)
                        if not any(target.is_relative_to(p) for p in outputs):
                            raise PipelineError("JUnit report must be inside an allowed output")
                        convert_jest_report(jest_json, target)
                    reports = junit_paths(worktree, command.junit_report, outputs)
                    if not reports:
                        raise PipelineError(
                            "invalid or absent JUnit test evidence: no report files"
                        )
                    if sum(p.stat().st_size for p in reports) > 8 * 1048576:
                        raise PipelineError("JUnit reports exceed safety limit")
                    counts = {
                        "total": 0,
                        "failed": 0,
                        "skipped": 0,
                        "failures": [],
                        "failure_details": [],
                    }
                    tree = ET.Element("testsuites")
                    for report in reports:
                        evidence = parse_junit(report)
                        for key in counts:
                            counts[key] += evidence[key]
                        tree.append(ET.fromstring(report.read_text()))
                    result.update(
                        counts,
                        passed=rc == 0 and counts["failed"] == 0,
                        status="passed" if rc == 0 and counts["failed"] == 0 else "failed",
                    )
                    # Retain one redacted, valid XML bundle for this command attempt.
                    for element in tree.iter():
                        element.attrib = {
                            key: sanitize(value) for key, value in element.attrib.items()
                        }
                        element.text = sanitize(element.text)
                        element.tail = sanitize(element.tail)
                    copy = directory / f"{command_id}.junit.xml"
                    write(copy, ET.tostring(tree, encoding="unicode"))
                    result.update(junit_path=str(copy), junit_hash=digest(copy.read_bytes()))
            except StopRequested:
                raise
            except (PipelineError, OSError) as error:
                result.update(
                    error=sanitize(str(error)) + (f"; {diagnostic}" if diagnostic else ""),
                    status="timeout" if "timeout" in str(error) else result["status"],
                )
            result["duration_seconds"] = time.monotonic() - started
            results.append(result)
        statuses = {r["status"] for r in results}
        status = next(
            (s for s in ["timeout", "unavailable", "parse_error", "failed"] if s in statuses),
            "passed",
        )
        return sanitize(
            {
                "passed": all(r["passed"] for r in results),
                "status": status,
                "duration_seconds": sum(r["duration_seconds"] for r in results),
                "commands": results,
            }
        )
