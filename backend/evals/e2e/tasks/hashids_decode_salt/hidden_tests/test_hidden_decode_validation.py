import pytest

from hashids import Hashids


@pytest.mark.parametrize("number", [5, 123, 99999])
def test_other_salt_is_rejected(number):
    assert Hashids("salt A").decode(Hashids("salt B").encode(number)) == ()


def test_other_min_length_is_rejected():
    short = Hashids("same salt").encode(7)
    assert Hashids("same salt", min_length=12).decode(short) == ()


@pytest.mark.parametrize("values", [(1,), (0, 0), (5, 10, 15), (4242,)])
def test_valid_hashids_still_decode(values):
    hashids = Hashids("salt A", min_length=8)
    assert hashids.decode(hashids.encode(*values)) == values
