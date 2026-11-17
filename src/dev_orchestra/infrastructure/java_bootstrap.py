"""Dependency-free Java assertions with host-readable JUnit evidence."""

import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main():
    home = os.environ.get("JAVA_HOME")
    java = str(Path(home) / "bin/java") if home else "java"
    javac = str(Path(home) / "bin/javac") if home else "javac"
    output = Path("build/java-tests")
    output.mkdir(parents=True, exist_ok=True)
    sources = sorted(Path("src/main/java").rglob("*.java"))
    tests = sorted(Path("src/test/java").rglob("*Test.java"))
    suite = ET.Element("testsuite")
    failed = 0
    if tests:
        compilation = subprocess.run(
            [javac, "-encoding", "UTF-8", "-d", str(output), *map(str, sources + tests)],
            capture_output=True,
            text=True,
            check=False,
        )
        if compilation.returncode:
            case = ET.SubElement(suite, "testcase", name="Java compilation")
            ET.SubElement(case, "error").text = compilation.stdout + compilation.stderr
            failed += 1
        else:
            for test in tests:
                package = re.search(r"^\s*package\s+([\w.]+)\s*;", test.read_text(), re.M)
                name = (package[1] + "." if package else "") + test.stem
                case = ET.SubElement(suite, "testcase", name=name)
                result = subprocess.run(
                    [java, "-ea", "-cp", str(output), name],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode:
                    ET.SubElement(case, "failure").text = result.stdout + result.stderr
                    failed += 1
    suite.set("tests", str(len(suite)))
    suite.set("failures", str(sum(c.find("failure") is not None for c in suite)))
    suite.set("errors", str(sum(c.find("error") is not None for c in suite)))
    ET.ElementTree(suite).write("build/junit.xml", encoding="utf-8", xml_declaration=True)
    return 0 if tests and not failed else 1


if __name__ == "__main__":
    sys.exit(main())
