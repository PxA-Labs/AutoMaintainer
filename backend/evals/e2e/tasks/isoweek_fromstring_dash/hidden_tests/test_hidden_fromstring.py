import pytest

from isoweek import Week


@pytest.mark.parametrize(
    "text, year, week",
    [("2011-W08", 2011, 8), ("2020-W53", 2020, 53), ("1999-W01", 1999, 1)],
)
def test_extended_form_is_accepted(text, year, week):
    assert Week.fromstring(text) == Week(year, week)
    assert Week.fromstring(text) == Week.fromstring(text.replace("-", ""))


@pytest.mark.parametrize("text", ["2011-08", "2011W8", "2011-W8", "W082011"])
def test_malformed_strings_still_raise(text):
    with pytest.raises(ValueError):
        Week.fromstring(text)
