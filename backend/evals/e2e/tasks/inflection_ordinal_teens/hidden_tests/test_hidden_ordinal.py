import pytest

import inflection


@pytest.mark.parametrize(
    "number, expected",
    [
        (11, "11th"),
        (12, "12th"),
        (13, "13th"),
        (111, "111th"),
        (112, "112th"),
        (1013, "1013th"),
        (-12, "-12th"),
    ],
)
def test_teens_take_th(number, expected):
    assert inflection.ordinalize(number) == expected


@pytest.mark.parametrize(
    "number, expected",
    [(21, "21st"), (22, "22nd"), (23, "23rd"), (102, "102nd"), (1003, "1003rd")],
)
def test_other_numbers_are_unchanged(number, expected):
    assert inflection.ordinalize(number) == expected
