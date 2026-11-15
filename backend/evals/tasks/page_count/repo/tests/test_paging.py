from listing.paging import page_bounds, page_count


def test_exact_multiple():
    assert page_count(10, 5) == 2


def test_page_bounds():
    assert page_bounds(1, 5) == (0, 5)
    assert page_bounds(3, 5) == (10, 15)
