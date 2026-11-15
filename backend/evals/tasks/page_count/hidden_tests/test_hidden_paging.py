import pytest

from listing.paging import page_count


def test_partial_last_page_is_counted():
    assert page_count(11, 5) == 3
    assert page_count(1, 5) == 1


def test_no_items_means_no_pages():
    assert page_count(0, 5) == 0


@pytest.mark.parametrize("per_page", [0, -3])
def test_invalid_page_size(per_page):
    with pytest.raises(ValueError):
        page_count(10, per_page)
