"""Offline test execution with read-only source and explicit output directories."""

import platform
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained
from dev_orchestra.infrastructure.process import environment, execute


def sandbox_command(worktree, outputs, scratch, argv):
    if platform.system() == "Darwin":
        # Seatbelt applies to every descendant, including repository test/build scripts.
        import json

        def sub(p):
            return "(subpath " + json.dumps(str(p.resolve())) + ")"

        profile = "(version 1)(deny default)(allow process*)(allow sysctl-read)(allow mach-lookup)(allow file-read*)"
        for name in [".ssh", ".aws", ".codex", ".config/gh", ".git-credentials"]:
            profile += "(deny file-read* " + sub(Path.home() / name) + ")"
        profile += (
            "(allow file-write* "
            + " ".join(sub(p) for p in [scratch, *outputs])
            + ' (literal "/dev/null"))'
        )
        for item in worktree.rglob("*"):
            if item.name.startswith(".env") or item.suffix in {".pem", ".key"}:
                profile += "(deny file-read* " + sub(item) + ")"
        return ["/usr/bin/sandbox-exec", "-p", profile, *argv]
    if platform.system() == "Linux" and shutil.which("bwrap"):
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
        }
    except (OSError, ET.ParseError, ValueError) as error:
        raise PipelineError(f"invalid or absent JUnit test evidence: {error}") from error


class TestRunner:
    __test__ = False

    def check(self, worktree, stop=lambda: None):
        with tempfile.TemporaryDirectory(prefix="devorchestra-probe-") as temp:
            scratch = Path(temp).resolve()
            argv = sandbox_command(worktree, [], scratch, ["/usr/bin/true"])
            rc, _ = execute(argv, worktree, 10, scratch / "probe.log", stop=stop)
            if rc:
                raise PipelineError("OS sandbox unavailable; refusing model calls and tests")

    def run(self, worktree, config, directory, stop=lambda: None):
        if not config.commands:
            raise PipelineError("configure at least one allowlisted test command")
        results = []
        directory.mkdir(parents=True, exist_ok=True)
        for command_id, command in config.commands.items():
            report = contained(worktree, command.junit_report)
            if not any(report.is_relative_to(contained(worktree, p)) for p in config.output_paths):
                raise PipelineError("JUnit report must be inside a configured output path")
            outputs = [contained(worktree, p) for p in config.output_paths]
            if any(p.is_file() for p in outputs):
                raise PipelineError("output path must be a directory")
            # Never grant write access to any tracked production/test file via output paths.
            from dev_orchestra.infrastructure.git import Git

            tracked = Git(worktree).call("ls-files").splitlines()
            if any((worktree / name).is_relative_to(p) for name in tracked for p in outputs):
                raise PipelineError("output path contains tracked source files")
            for p in outputs:
                p.mkdir(parents=True, exist_ok=True)
            report.unlink(missing_ok=True)
            with tempfile.TemporaryDirectory(prefix="devorchestra-test-") as temp:
                scratch = Path(temp).resolve()
                env = environment() | {
                    "HOME": str(scratch),
                    "TMPDIR": str(scratch),
                    "TMP": str(scratch),
                    "TEMP": str(scratch),
                }
                argv = sandbox_command(worktree, outputs, scratch, command.argv)
                rc, _ = execute(
                    argv,
                    worktree,
                    command.timeout_seconds,
                    directory / f"{command_id}.log",
                    stop=stop,
                    env=env,
                )
            counts = parse_junit(report)
            results.append(
                {
                    "command_id": command_id,
                    "argv": command.argv,
                    "exit_code": rc,
                    **counts,
                    "passed": rc == 0 and counts["failed"] == 0,
                }
            )
        return {"passed": all(r["passed"] for r in results), "commands": results}
