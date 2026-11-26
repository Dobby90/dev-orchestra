import unittest

from usage import total_bytes


class UsageTest(unittest.TestCase):
    def test_typical_window(self):
        events = [
            {"at": "2024-05-01T11:20:00+00:00", "bytes": 9},
            {"at": "2024-05-01T11:40:00+00:00", "bytes": 12},
            {"at": "2024-05-01T13:00:00+00:00", "bytes": 30},
        ]
        self.assertEqual(total_bytes(events, "2024-05-01T11:00Z", "2024-05-01T12:00Z"), 21)

    def test_no_events(self):
        self.assertEqual(total_bytes([], "2024-05-01T11:00Z", "2024-05-01T12:00Z"), 0)
