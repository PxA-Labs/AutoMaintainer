from uuid import UUID

from shortuuid.main import ShortUUID


def test_round_trip_full_length_uuid():
    su = ShortUUID()
    u = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
    assert su.decode(su.encode(u)) == u
    assert len(su.encode(u)) == 22


def test_alphabet_is_sorted_and_unique():
    assert ShortUUID("cbaa").get_alphabet() == "abc"
