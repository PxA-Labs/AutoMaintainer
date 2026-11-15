"""Shipping cost calculation."""

_CARRIERS = {}


def register_carrier(name: str, rate_fn, *, replace: bool = False) -> None:
    """Make ``name`` available to :func:`shipping_cost`."""
    if name in _CARRIERS and not replace:
        raise ValueError(f"carrier already registered: {name}")
    _CARRIERS[name] = rate_fn


def shipping_cost(carrier: str, weight_kg: float) -> float:
    """Return the cost of shipping ``weight_kg`` with ``carrier``."""
    if weight_kg < 0:
        raise ValueError("weight must not be negative")
    try:
        rate_fn = _CARRIERS[carrier]
    except KeyError:
        raise ValueError(f"unknown carrier: {carrier}") from None
    return rate_fn(weight_kg)


def _post(weight_kg: float) -> float:
    return 4.5 if weight_kg <= 2 else 4.5 + (weight_kg - 2) * 1.2


register_carrier("post", _post)
register_carrier("courier", lambda weight_kg: 9.0 + weight_kg * 0.8)
register_carrier("freight", lambda weight_kg: max(60.0, weight_kg * 0.35))
