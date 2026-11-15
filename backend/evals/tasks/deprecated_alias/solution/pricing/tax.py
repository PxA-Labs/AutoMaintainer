"""Value-added tax helpers."""

import warnings

VAT_RATE = 0.2


def vat_for(amount: float, rate: float = VAT_RATE) -> float:
    """Return the VAT owed on ``amount`` at ``rate``, rounded to cents."""
    return round(amount * rate, 2)


def calc_vat(amount: float) -> float:
    """Deprecated alias of :func:`vat_for` at the default rate."""
    warnings.warn(
        "calc_vat is deprecated; use vat_for", DeprecationWarning, stacklevel=2
    )
    return vat_for(amount)
