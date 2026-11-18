from datetime import date

from boltons.timeutils import daterange


def test_exclusive_by_default():
    days = list(daterange(date(2015, 12, 25), date(2015, 12, 28)))
    assert days == [date(2015, 12, 25), date(2015, 12, 26), date(2015, 12, 27)]


def test_descending_inclusive():
    days = list(daterange(date(2020, 1, 3), date(2020, 1, 1), step=-1, inclusive=True))
    assert days == [date(2020, 1, 3), date(2020, 1, 2), date(2020, 1, 1)]
