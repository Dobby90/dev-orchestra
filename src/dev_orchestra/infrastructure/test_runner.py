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
from dev_orchestra.infrastructure.jest_outputs import cache_files, cache_path, generated_outputs
from dev_orchestra.infrastructure.process import (
    environment,
    execute,
    execution_directory,
    execution_scope,
    sanitize,
)
from dev_orchestra.infrastructure.secret_files import denied_subpaths


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
        for item in denied_subpaths(worktree):
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


def junit_bundle(reports):
    """Use the same bounded, redacted XML representation for retention and cleanup."""
    if sum(p.stat().st_size for p in reports) > 8 * 1048576:
        raise PipelineError("JUnit reports exceed safety limit")
    tree = ET.Element("testsuites")
    for report in reports:
        parse_junit(report)
        tree.append(ET.fromstring(report.read_text()))
    for element in tree.iter():
        element.attrib = {key: sanitize(value) for key, value in element.attrib.items()}
        element.text = sanitize(element.text)
        element.tail = sanitize(element.tail)
    return ET.tostring(tree, encoding="unicode")


def validation_outputs(worktree, paths):
    from dev_orchestra.infrastructure.git import Git

    outputs = [contained(worktree, path) for path in paths]
    if any(p.is_file() for p in outputs):
        raise PipelineError("output path must be a directory")
    git = Git(worktree)
    tracked = [name for name in git.call("ls-files", "-z").split("\0") if name]
    if any((worktree / name).is_relative_to(p) for name in tracked for p in outputs):
        raise PipelineError("output path contains tracked source files")
    for path in outputs:
        path.mkdir(parents=True, exist_ok=True)
    return outputs, git


def result_record(command_id, command):
    return {
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


def scratch_environment(scratch):
    return environment() | {
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        # Chromium on macOS ignores TMPDIR for some temporary files.
        "MAC_CHROMIUM_TMPDIR": str(scratch),
        "JAVA_TOOL_OPTIONS": f'-Duser.home="{scratch}" -Djava.io.tmpdir="{scratch}"',
    }


def loopback_slot(command, directory, stop):
    from dev_orchestra.infrastructure.coordination import host_root, slot

    if command.network == "loopback":
        return slot(host_root() / "loopback", execution_directory() or directory, stop, limit=1)
    return nullcontext()


def run_gradle_batch(worktree, batch, directory, paths, bindings, gradle_caches, trace, stop):
    from dev_orchestra.infrastructure.gradle_assemble import (
        batch_adapter,
        batch_outcomes,
        compatible_caches,
    )
    from dev_orchestra.infrastructure.gradle_worktree_caches import gradle_cache_roots
    from dev_orchestra.infrastructure.java import GRADLE_DIRECTORY_TASK
    from dev_orchestra.infrastructure.runtime_adapters import runtime_command

    first = batch[0]
    command = first.command
    outputs, _ = validation_outputs(worktree, paths)
    if any(
        not any(cache.is_relative_to(p) for p in outputs) for item in batch for cache in item.caches
    ):
        return None
    with (
        loopback_slot(command, directory, stop),
        execution_scope(execution_directory() or directory),
        tempfile.TemporaryDirectory(prefix="devorchestra-test-") as temp,
    ):
        if not all(compatible_caches(first, item) for item in batch[1:]):
            return None
        scratch = Path(temp).resolve()
        env = scratch_environment(scratch) | command.env | first.environment
        env["GRADLE_USER_HOME"] = str(first.caches[0])
        runtime = command.model_copy(
            update={
                "argv": [
                    arg
                    for arg in command.argv
                    if arg
                    not in {
                        GRADLE_DIRECTORY_TASK + "assemble",
                        "--console=plain",
                        "--no-configuration-cache",
                    }
                ]
            }
        )
        try:
            if bindings:
                env.update(bindings.environment_for(first.id, stop))
            argv, adapters = runtime_command(worktree, runtime, directory, env)
            path, sha, marker = batch_adapter(batch, directory)
        except StopRequested:
            raise
        except (PipelineError, OSError):
            return None  # No process has started; let each command report its own setup result.
        argv += [
            "--continue",
            "--console=plain",
            "--no-configuration-cache",
            "--init-script",
            str(path),
        ]
        adapters.append({"adapter": "gradle-assemble-batch", "sha256": sha})
        sandbox = sandbox_command(worktree, outputs, scratch, argv, command.network)
        roots = gradle_cache_roots(worktree, [command], paths) if gradle_caches else set()
        log = directory / trace["log"]
        try:
            with gradle_caches.record(roots, stop) if roots else nullcontext():
                rc, output = execute(
                    sandbox, first.cwd, command.timeout_seconds, log, stop=stop, env=env
                )
        except PipelineError as error:
            if type(error) is PipelineError and str(error) in {
                "process timeout",
                "process output exceeded 8 MiB safety limit",
            }:
                return None  # execute confirmed cleanup; retry with per-command limits.
            raise
    outcomes = batch_outcomes(batch, marker, output, rc)
    if outcomes is None:
        return None
    trace["exit_code"] = rc
    results = []
    for item in batch:
        task, completed, failed = outcomes[item.id]
        result = result_record(item.id, item.command)
        result.update(
            passed=not failed,
            status="failed" if failed else "passed",
            error=(
                f"Gradle failed tasks: {', '.join(failed)} (see {log.name})" if failed else None
            ),
            runtime_argv=argv,
            runtime_adapters=adapters,
            runtime_batch={
                "log": log.name,
                "task": task,
                "task_completed": completed,
                "failed_tasks": failed,
                "exit_code": rc,
            },
            duration_seconds=None,
        )
        results.append(result)
    return results


class TestRunner:
    __test__ = False

    def __init__(self, *, preflight=None):
        self.preflight = preflight

    def host_tools(self, directory, stop=lambda: None):
        from dev_orchestra.infrastructure.containers import host_tools

        return host_tools(directory, stop)

    def prepare(self, worktree, config, directory, stop=lambda: None):
        from dev_orchestra.infrastructure.dependencies import prepare_dependencies

        prepare_dependencies(worktree, config, directory, stop)
        self.preflight = directory

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
        from dev_orchestra.infrastructure.gradle_assemble import assemble_batches
        from dev_orchestra.infrastructure.gradle_worktree_caches import (
            GradleWorktreeCaches,
            gradle_cache_roots,
        )
        from dev_orchestra.infrastructure.java_cache import GradleCacheBindings

        bindings = GradleCacheBindings(self.preflight, worktree) if self.preflight else None
        gradle_caches = GradleWorktreeCaches(self.preflight, worktree) if self.preflight else None
        batches = assemble_batches(worktree.resolve(), config.commands, directory, bindings, stop)
        results = []
        directory.mkdir(parents=True, exist_ok=True)
        observation = {"started_at": time.time(), "status": "incomplete", "commands": []}
        observation_path = directory / ("test-invocation-" + uuid.uuid4().hex + ".json")

        def observe():
            try:
                write(observation_path, observation)
            except OSError:
                pass  # Measurements never replace actual JUnit or execution evidence.

        def record(result):
            results.append(result)
            observation["commands"].append(
                {key: result[key] for key in ["command_id", "status", "duration_seconds"]}
            )
            observe()

        observe()
        batched = set()
        for command_id, command in config.commands.items():
            if command_id in batched:
                continue
            if batch := batches.get(command_id):
                started = time.monotonic()
                trace = {
                    "command_ids": [item.id for item in batch],
                    "log": f"gradle-assemble-{uuid.uuid4().hex}.log",
                }
                combined = run_gradle_batch(
                    worktree,
                    batch,
                    directory,
                    config.output_paths,
                    bindings,
                    gradle_caches,
                    trace,
                    stop,
                )
                trace.update(duration_seconds=time.monotonic() - started, used=combined is not None)
                observation.setdefault("batches", []).append(trace)
                observe()
                if combined is not None:
                    batched.update(trace["command_ids"])
                    for result in combined:
                        record(result)
                    continue
            cwd = (
                worktree
                if command.working_directory == "."
                else contained(worktree, command.working_directory)
            )
            outputs, git = validation_outputs(worktree, config.output_paths)
            from dev_orchestra.infrastructure.runtime_adapters import (
                convert_jest_report,
                gradle_directory_command,
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
            result = result_record(command_id, command)
            diagnostic = ""
            try:
                with (
                    loopback_slot(command, directory, stop),
                    execution_scope(execution_directory() or directory),
                    tempfile.TemporaryDirectory(prefix="devorchestra-test-") as temp,
                ):
                    scratch = Path(temp).resolve()
                    env = scratch_environment(scratch) | command.env
                    from dev_orchestra.infrastructure.containers import run_arguments, toolchain
                    from dev_orchestra.infrastructure.java import java_environment

                    prepared = toolchain(worktree, directory, stop)
                    if prepared:
                        runtime_argv, adapters, files = gradle_directory_command(
                            command, command.argv, directory
                        )
                        argv, owner_env = run_arguments(
                            worktree,
                            worktree,
                            outputs,
                            scratch,
                            runtime_argv,
                            prepared,
                            directory,
                            cwd=cwd,
                            extra_env=command.env,
                            adapters=files,
                        )
                        env.update(owner_env)
                        result["runtime_argv"] = runtime_argv
                        result["runtime_adapters"] = ["docker", *adapters]
                        result["container_environment"] = {
                            k: v for k, v in prepared.items() if k != "docker"
                        }
                    else:
                        env.update(java_environment(worktree, command))
                        if bindings and "GRADLE_USER_HOME" in env:
                            env.update(bindings.environment_for(command_id, stop))
                        from dev_orchestra.infrastructure.node_runtime import (
                            node_environment,
                            node_project,
                        )

                        if root := node_project(worktree, command):
                            node_env, runtime = node_environment(root, directory, stop)
                            env.update(node_env)
                            result["node_runtime"] = runtime
                        runtime_argv, adapters = runtime_command(worktree, command, directory, env)
                        result["runtime_argv"] = runtime_argv
                        result["runtime_adapters"] = adapters
                        options = (
                            {"browser": True} if "PLAYWRIGHT_BROWSERS_PATH" in command.env else {}
                        )
                        argv = sandbox_command(
                            worktree, outputs, scratch, runtime_argv, command.network, **options
                        )
                    jest_cache = cache_path(worktree, command, outputs) if jest_json else None
                    try:
                        cache_before = cache_files(git, jest_cache)
                    except (OSError, PipelineError):
                        cache_before = None
                    gradle_roots = (
                        gradle_cache_roots(worktree, [command], config.output_paths)
                        if gradle_caches
                        else set()
                    )
                    with (
                        gradle_caches.record(gradle_roots, stop) if gradle_roots else nullcontext()
                    ):
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
                    for report in reports:
                        evidence = parse_junit(report)
                        for key in counts:
                            counts[key] += evidence[key]
                    result.update(
                        counts,
                        passed=rc == 0 and counts["failed"] == 0,
                        status="passed" if rc == 0 and counts["failed"] == 0 else "failed",
                    )
                    # Retain one redacted, valid XML bundle for this command attempt.
                    copy = directory / f"{command_id}.junit.xml"
                    write(copy, junit_bundle(reports))
                    result.update(junit_path=str(copy), junit_hash=digest(copy.read_bytes()))
                    if jest_json:
                        try:
                            result["jest_outputs"] = generated_outputs(
                                git, jest_json, jest_cache, cache_before
                            )
                        except (OSError, PipelineError):
                            pass  # Unidentified files remain protected during cleanup.
            except StopRequested:
                raise
            except (PipelineError, OSError) as error:
                result.update(
                    error=sanitize(str(error)) + (f"; {diagnostic}" if diagnostic else ""),
                    status="timeout" if "timeout" in str(error) else result["status"],
                )
            result["duration_seconds"] = time.monotonic() - started
            record(result)
        statuses = {r["status"] for r in results}
        status = next(
            (s for s in ["timeout", "unavailable", "parse_error", "failed"] if s in statuses),
            "passed",
        )
        observation.update(status=status, finished_at=time.time())
        observe()
        return sanitize(
            {
                "passed": all(r["passed"] for r in results),
                "status": status,
                "duration_seconds": sum(r["duration_seconds"] or 0 for r in results)
                + sum(batch["duration_seconds"] for batch in observation.get("batches", [])),
                "commands": results,
            }
        )
