from hashids import Hashids


def test_round_trip():
    hashids = Hashids("this is my salt")
    assert hashids.decode(hashids.encode(1, 2, 3)) == (1, 2, 3)


def test_encode_rejects_negative_numbers():
    assert Hashids().encode(-1) == ""
