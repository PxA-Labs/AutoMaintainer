import warnings

import pytest

from pricing import tax


@pytest.mark.parametrize(
    "amount, rate, expected", [(100, 0.2, 20.0), (19.99, 0.07, 1.4), (50, 0, 0)]
)
def test_vat_for_with_rate(amount, rate, expected):
    assert tax.vat_for(amount, rate) == expected


def test_vat_for_default_rate_and_no_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert tax.vat_for(10.555) == 2.11
        assert tax.vat_for(amount=40) == 8.0


def test_calc_vat_warns_every_time_and_matches():
    for _ in range(2):
        with pytest.warns(DeprecationWarning):
            assert tax.calc_vat(33.33) == tax.vat_for(33.33)
