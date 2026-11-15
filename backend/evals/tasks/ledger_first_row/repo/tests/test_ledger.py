from ledger import parse_rows


def test_parse_rows_uses_header():
    rows = parse_rows("category,amount\nfood,3.5\nrent,100\n")
    assert rows == [
        {"category": "food", "amount": "3.5"},
        {"category": "rent", "amount": "100"},
    ]
