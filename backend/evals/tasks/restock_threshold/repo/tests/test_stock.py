from shop.stock import needs_restock, restock_list


def test_low_stock_is_reordered():
    assert needs_restock(2, 5)


def test_plenty_of_stock_is_not_reordered():
    assert not needs_restock(9, 5)


def test_restock_list_is_sorted():
    assert restock_list({"pears": 0, "apples": 1, "kiwis": 20}, 3) == [
        "apples",
        "pears",
    ]
