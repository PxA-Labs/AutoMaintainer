from datetime import date

from boltons.timeutils import daterange


def test_inclusive_monthly_steps():
    days = list(
        daterange(date(2017, 5, 1), date(2017, 8, 1), step=(0, 1, 0), inclusive=True)
    )
    assert days == [
        date(2017, 5, 1),
        date(2017, 6, 1),
        date(2017, 7, 1),
        date(2017, 8, 1),
    ]


def test_inclusive_daily_and_uneven_steps():
    assert list(daterange(date(2020, 1, 1), date(2020, 1, 3), inclusive=True)) == [
        date(2020, 1, 1),
        date(2020, 1, 2),
        date(2020, 1, 3),
    ]
    assert list(
        daterange(date(2020, 1, 1), date(2020, 1, 4), step=2, inclusive=True)
    ) == [date(2020, 1, 1), date(2020, 1, 3)]
    assert list(daterange(date(2020, 1, 1), date(2020, 1, 3))) == [
        date(2020, 1, 1),
        date(2020, 1, 2),
    ]
