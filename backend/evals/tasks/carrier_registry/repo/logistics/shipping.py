"""Shipping cost calculation."""


def shipping_cost(carrier: str, weight_kg: float) -> float:
    """Return the cost of shipping ``weight_kg`` with ``carrier``."""
    if weight_kg < 0:
        raise ValueError("weight must not be negative")
    if carrier == "post":
        return 4.5 if weight_kg <= 2 else 4.5 + (weight_kg - 2) * 1.2
    elif carrier == "courier":
        return 9.0 + weight_kg * 0.8
    elif carrier == "freight":
        return max(60.0, weight_kg * 0.35)
    raise ValueError(f"unknown carrier: {carrier}")
