from datetime import date

from isoweek import Week


def test_str_and_fromstring_compact_form():
    assert str(Week(2011, 8)) == "2011W08"
    assert Week.fromstring("2011W08") == Week(2011, 8)


def test_monday_of_a_year_starting_midweek():
    assert Week(2020, 1).monday() == date(2019, 12, 30)
    assert Week(2019, 1).monday() == date(2018, 12, 31)


def test_adding_weeks():
    assert Week(2020, 1) + 1 == Week(2020, 2)
