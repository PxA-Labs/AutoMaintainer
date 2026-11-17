import pytest

from pytimeparse.timeparse import timeparse


@pytest.mark.parametrize(
    "text, expected",
    [("- 1 minute", -60), ("-1:24", -84), ("-2h", -7200), ("-1.5s", -1.5)],
)
def test_minus_negates(text, expected):
    assert timeparse(text) == expected


@pytest.mark.parametrize("text, expected", [("+ 1 minute", 60), ("+5s", 5)])
def test_plus_stays_positive(text, expected):
    assert timeparse(text) == expected
