"""Host-owned runtime adapters that leave source and validation goals unchanged."""

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from dev_orchestra.domain.models import PipelineError
from dev_orchestra.infrastructure.files import contained, digest

GRADLE_INIT = r"""
allprojects { project ->
    project.tasks.withType(org.gradle.api.tasks.testing.Test).configureEach { task ->
        task.doFirst {
            def files = task.classpath.files
            def mockito = files.find { it.name ==~ /mockito-core-[0-9].*\.jar/ }
            if (mockito != null && !task.jvmArgs.any { it.startsWith('-javaagent:') && (it.contains('mockito') || it.contains('byte-buddy')) }) {
                def premain = null
                new java.util.jar.JarFile(mockito).withCloseable { jar ->
                    premain = jar.manifest?.mainAttributes?.getValue('Premain-Class')
                }
                def agent = premain ? mockito : files.find { it.name ==~ /byte-buddy-agent-[0-9].*\.jar/ }
                if (agent != null) task.jvmArgs('-javaagent:' + agent.absolutePath)
            }
        }
    }
}
"""

VUE_HOOK = r"""
const Module = require('node:module');
const load = Module._load;
Module._load = function(name, parent, isMain) {
  const value = load.apply(this, arguments);
  if (name !== 'eslint-webpack-plugin' || typeof value !== 'function') return value;
  return new Proxy(value, {construct(target, args, next) {
    return Reflect.construct(target, [{...(args[0] || {}), fix: false, cache: false}, ...args.slice(1)], next);
  }});
};
"""


def setup_file(directory, name, content):
    sha = digest(content.encode())
    path = directory / (sha[:16] + "-" + name)
    if path.is_symlink():
        raise PipelineError("runtime adapter symlinks denied")
    path.write_text(content)
    return path, sha


def runtime_command(worktree, command, directory, env):
    from dev_orchestra.infrastructure.java import java_argv, java_tool

    argv = list(command.argv)
    evidence = []
    if java_tool(command):
        argv = java_argv(worktree, command)
        if java_tool(command) == "gradle":
            from dev_orchestra.infrastructure.java_native import native_init

            path, sha = setup_file(
                directory, "runtime.gradle", GRADLE_INIT + native_init(worktree, command)
            )
            argv += ["--init-script", str(path)]
            evidence.append({"adapter": "gradle-test-agent", "sha256": sha})
    if Path(argv[0]).name in {"npm", "yarn", "pnpm"} and "run" in argv:
        index = argv.index("run") + 1
        cwd = worktree / command.working_directory
        manifest = contained(worktree, str((cwd / "package.json").relative_to(worktree)))
        if manifest.is_file() and manifest.stat().st_size <= 2**20 and index < len(argv):
            try:
                scripts = json.loads(manifest.read_text()).get("scripts", {})
                if not isinstance(scripts, dict):
                    raise ValueError("scripts must be an object")
            except (ValueError, AttributeError) as error:
                raise PipelineError("invalid package.json for runtime adapter") from error
            script = scripts.get(argv[index], "")
            if re.search(r"(?:^|\s)vue-cli-service\s+build(?:\s|$)", script):
                path, sha = setup_file(directory, "vue-readonly.cjs", VUE_HOOK)
                env["NODE_OPTIONS"] = (
                    env.get("NODE_OPTIONS", "") + " --require " + json.dumps(str(path))
                ).strip()
                evidence.append({"adapter": "vue-eslint-readonly", "sha256": sha})
    return argv, evidence


def jest_json_path(worktree, command):
    """Only explicit generated JSON reports can be converted to JUnit evidence."""
    if command.kind != "test" or "--json" not in command.argv or "--ci" not in command.argv:
        return None
    values = [arg.partition("=")[2] for arg in command.argv if arg.startswith("--outputFile=")]
    if len(values) != 1 or not values[0].endswith("-jest.json"):
        return None
    cwd = Path(command.working_directory)
    return contained(worktree, str(cwd / values[0]))


def convert_jest_report(source, target):
    try:
        _convert_jest_report(source, target)
    except (ValueError, KeyError, TypeError) as error:
        raise PipelineError("invalid Jest JSON test evidence") from error


def _convert_jest_report(source, target):
    if not source.is_file() or source.stat().st_size > 8 * 2**20:
        raise PipelineError("Jest JSON report missing or oversized")
    result = json.loads(source.read_text())
    suites = ET.Element("testsuites")
    if result.get("success") is False or result.get("numRuntimeErrorTestSuites", 0):
        suites.set("errors", "1")
    counts = {"passed": 0, "failed": 0, "pending": 0, "todo": 0}
    for file in result["testResults"]:
        assertions = file["assertionResults"]
        suite = ET.SubElement(suites, "testsuite", name=str(file["name"]))
        for assertion in assertions:
            status = assertion["status"]
            if status not in counts:
                raise PipelineError("unrecognized Jest assertion status")
            counts[status] += 1
            case = ET.SubElement(
                suite,
                "testcase",
                name=str(assertion["fullName"]),
                classname=str(file["name"]),
                time=str((assertion.get("duration") or 0) / 1000),
            )
            if status == "failed":
                ET.SubElement(case, "failure").text = "\n".join(assertion["failureMessages"])
            elif status in {"pending", "todo"}:
                ET.SubElement(case, "skipped")
        if file.get("status") == "failed":
            suite.set("errors", "1")
            ET.SubElement(suite, "error").text = str(
                file.get("message", "Jest suite failed before assertions")
            )
    expected = {
        "passed": result["numPassedTests"],
        "failed": result["numFailedTests"],
        "pending": result["numPendingTests"],
        "todo": result.get("numTodoTests", 0),
    }
    if counts != expected or sum(counts.values()) != result["numTotalTests"]:
        raise PipelineError("Jest assertion totals do not match JSON report summary")
    target.write_text(ET.tostring(suites, encoding="unicode"))
