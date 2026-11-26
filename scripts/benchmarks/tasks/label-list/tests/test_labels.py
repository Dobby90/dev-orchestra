import unittest

from labels import parse_labels


class LabelsTest(unittest.TestCase):
    def test_default_keeps_repeated_labels(self):
        self.assertEqual(parse_labels(" api, ,API, api "), ["api", "API", "api"])

    def test_empty_input(self):
        self.assertEqual(parse_labels(" , "), [])
