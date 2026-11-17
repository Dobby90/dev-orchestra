"""Dependency-free unittest execution with JUnit evidence for new projects."""

import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


class JUnitResult(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        self.case = ET.SubElement(self.suite, "testcase", name=test.id())

    def addFailure(self, test, err):
        super().addFailure(test, err)
        ET.SubElement(self.case, "failure").text = self._exc_info_to_string(err, test)

    def addError(self, test, err):
        super().addError(test, err)
        ET.SubElement(self.case, "error").text = self._exc_info_to_string(err, test)

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        ET.SubElement(self.case, "skipped", message=reason)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            ET.SubElement(
                self.case, "failure", message=str(subtest)
            ).text = self._exc_info_to_string(err, test)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        ET.SubElement(self.case, "skipped", message="expected failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        ET.SubElement(self.case, "failure", message="unexpected success")


def main():
    sys.path.insert(0, str(Path.cwd()))
    suite = ET.Element("testsuite")

    def result_factory(*args):
        result = JUnitResult(*args)
        result.suite = suite
        return result

    tests = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(resultclass=result_factory).run(tests)
    suite.set("tests", str(result.testsRun))
    suite.set("failures", str(sum(case.find("failure") is not None for case in suite)))
    suite.set("errors", str(sum(case.find("error") is not None for case in suite)))
    suite.set("skipped", str(len(result.skipped) + len(result.expectedFailures)))
    Path("build").mkdir(exist_ok=True)
    ET.ElementTree(suite).write("build/junit.xml", encoding="utf-8", xml_declaration=True)
    return (
        0
        if result.wasSuccessful()
        and result.testsRun > len(result.skipped) + len(result.expectedFailures)
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
