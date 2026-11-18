import pytest

from hashids import Hashids


@pytest.mark.parametrize("text", ["zz", "12g4", "0x1f", "507f1f77bcf86cd79943901z"])
def test_invalid_hex_gives_empty_string(text):
    assert Hashids("salt").encode_hex(text) == ""


@pytest.mark.parametrize("text", ["507f1f77bcf86cd799439011", "deadbeef", "f00"])
def test_valid_hex_round_trips(text):
    hashids = Hashids("salt")
    assert hashids.decode_hex(hashids.encode_hex(text)) == text
