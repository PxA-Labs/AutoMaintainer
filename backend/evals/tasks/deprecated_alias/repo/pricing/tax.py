"""Value-added tax helpers."""

VAT_RATE = 0.2


def calc_vat(amount: float) -> float:
    """Return the VAT owed on ``amount``, rounded to cents."""
    return round(amount * VAT_RATE, 2)
