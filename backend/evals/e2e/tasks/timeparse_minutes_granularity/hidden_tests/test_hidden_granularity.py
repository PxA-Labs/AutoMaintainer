import pytest

from pytimeparse.timeparse import timeparse


@pytest.mark.parametrize("text, expected", [("1:30", 5400), ("2:05", 7500)])
def test_minutes_granularity(text, expected):
    assert timeparse(text, granularity="minutes") == expected


@pytest.mark.parametrize(
    "text, expected", [("1:30:00", 5400), ("1:30.5", 90.5), ("1h 30m", 5400)]
)
def test_unambiguous_inputs_unchanged(text, expected):
    assert timeparse(text, granularity="minutes") == expected
    assert timeparse(text) == expected
