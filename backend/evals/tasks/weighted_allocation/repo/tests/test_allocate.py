from billing.allocate import split_evenly


def test_split_evenly_gives_extra_cents_to_first_shares():
    assert split_evenly(100, 3) == [34, 33, 33]


def test_split_evenly_exact():
    assert split_evenly(90, 3) == [30, 30, 30]
