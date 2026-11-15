import pytest

from iniconf.parser import ConfigError, parse_config


def test_all_required_present():
    text = "host = db\nport = 1\nuser = me\n"
    assert parse_config(text, required=["port", "host"]) == {
        "host": "db",
        "port": "1",
        "user": "me",
    }


def test_every_missing_key_is_reported_sorted():
    with pytest.raises(ConfigError) as info:
        parse_config("port = 1\n", required=("user", "host", "port", "db"))
    assert info.value.missing == ["db", "host", "user"]


def test_required_accepts_any_iterable():
    with pytest.raises(ConfigError) as info:
        parse_config("", required=iter(["token"]))
    assert info.value.missing == ["token"]


def test_required_is_keyword_only():
    with pytest.raises(TypeError):
        parse_config("a = 1", ["a"])
