from timefmt.durations import parse_duration


def test_hours_and_minutes():
    assert parse_duration("1h30m") == 5400


def test_minutes_only():
    assert parse_duration("45m") == 2700
