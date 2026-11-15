import pytest

from timefmt.durations import parse_duration


def test_seconds_unit():
    assert parse_duration("2m15s") == 135


def test_all_units():
    assert parse_duration("1h0m5s") == 3605


@pytest.mark.parametrize("text", ["", "90", "5d", "1h30"])
def test_malformed_input_raises(text):
    with pytest.raises(ValueError):
        parse_duration(text)
