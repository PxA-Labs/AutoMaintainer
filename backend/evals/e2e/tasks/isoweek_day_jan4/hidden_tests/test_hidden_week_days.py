from datetime import date, timedelta

import pytest

from isoweek import Week


@pytest.mark.parametrize(
    "year, week, monday",
    [
        (2021, 1, date(2021, 1, 4)),
        (2016, 1, date(2016, 1, 4)),
        (2022, 1, date(2022, 1, 3)),
        (2011, 8, date(2011, 2, 21)),
        (2010, 52, date(2010, 12, 27)),
    ],
)
def test_monday_follows_iso_8601(year, week, monday):
    assert Week(year, week).monday() == monday
    assert Week(year, week).sunday() == monday + timedelta(days=6)


def test_every_date_is_in_its_own_week():
    start = date(2020, 12, 1)
    for offset in range(800):
        day = start + timedelta(days=offset)
        week = Week.withdate(day)
        assert week.contains(day), day
        assert week.days()[day.weekday()] == day


def test_week_arithmetic_across_years():
    assert Week(2021, 1) - Week(2020, 53) == 1
    assert Week(2021, 1) + 52 == Week(2022, 1)
