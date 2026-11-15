from ledger import parse_rows, total_by_category


def test_first_transaction_is_counted():
    rows = parse_rows("category,amount\nfood,3.5\nrent,100\nfood,1.5\n")
    assert total_by_category(rows) == {"food": 5.0, "rent": 100.0}


def test_single_transaction():
    assert total_by_category([{"category": "fun", "amount": "7"}]) == {"fun": 7.0}


def test_no_transactions():
    assert total_by_category([]) == {}
