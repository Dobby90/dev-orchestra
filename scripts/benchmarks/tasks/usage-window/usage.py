from datetime import datetime


def _clock(value):
    return datetime.fromisoformat(value).replace(tzinfo=None)


def total_bytes(events, start, end):
    lower, upper = _clock(start), _clock(end)
    return sum(event["bytes"] for event in events if lower <= _clock(event["at"]) <= upper)
