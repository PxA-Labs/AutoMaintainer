from pricing.tax import calc_vat


def test_calc_vat_rounds_to_cents():
    assert calc_vat(10.555) == 2.11


def test_calc_vat_zero():
    assert calc_vat(0) == 0
