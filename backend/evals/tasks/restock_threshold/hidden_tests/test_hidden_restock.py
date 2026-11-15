from shop.stock import needs_restock, restock_list


def test_quantity_equal_to_threshold_is_reordered():
    assert needs_restock(5, 5)


def test_one_above_threshold_is_not_reordered():
    assert not needs_restock(6, 5)


def test_restock_list_includes_items_at_threshold():
    assert restock_list({"bolts": 5, "nuts": 6, "washers": 4}, 5) == [
        "bolts",
        "washers",
    ]
