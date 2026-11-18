from boltons.strutils import format_int_list, parse_int_list


def test_parse_int_list():
    assert parse_int_list("1,3,5-8,10-11,15") == [1, 3, 5, 6, 7, 8, 10, 11, 15]


def test_format_int_list_with_trailing_single():
    assert format_int_list([1, 3, 5, 6, 7, 8, 10, 11, 15]) == "1,3,5-8,10-11,15"
