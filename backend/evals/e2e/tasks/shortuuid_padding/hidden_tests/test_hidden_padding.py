from uuid import UUID

import pytest

from shortuuid.main import ShortUUID


@pytest.mark.parametrize("value", [0, 1, 56, 57, 2**64, 2**100 + 12345])
def test_small_uuids_round_trip(value):
    su = ShortUUID()
    u = UUID(int=value)
    encoded = su.encode(u)
    assert len(encoded) == 22
    assert su.decode(encoded) == u


def test_padding_is_most_significant():
    su = ShortUUID()
    assert su.encode(UUID(int=0)) == "2" * 22
    assert su.encode(UUID(int=1)) == "2" * 21 + "3"
    assert su.decode(su.encode(UUID(int=7), pad_length=30)) == UUID(int=7)
