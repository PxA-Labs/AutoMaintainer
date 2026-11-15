import pytest

from netcfg.ports import parse_port


@pytest.mark.parametrize(
    "value, expected", [(" 8080 ", 8080), ("1", 1), (65535, 65535), ("\t22\n", 22)]
)
def test_valid(value, expected):
    result = parse_port(value)
    assert result == expected and type(result) is int


@pytest.mark.parametrize(
    "value",
    [
        0,
        65536,
        -1,
        "0",
        "65536",
        "-1",
        "+80",
        "80.0",
        80.0,
        "",
        "  ",
        "8o",
        True,
        False,
        None,
        "²",
    ],
)
def test_invalid(value):
    with pytest.raises(ValueError):
        parse_port(value)
