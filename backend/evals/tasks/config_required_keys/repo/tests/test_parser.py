import pytest

from iniconf.parser import ConfigError, parse_config


def test_parses_pairs_and_skips_comments():
    text = "# db\nhost = localhost\n\nport=5432\n"
    assert parse_config(text) == {"host": "localhost", "port": "5432"}


def test_malformed_line_raises():
    with pytest.raises(ConfigError, match="line 2"):
        parse_config("a = 1\njunk\n")
