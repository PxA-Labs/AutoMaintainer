"""Stock level helpers for a small shop inventory."""


def needs_restock(quantity: int, threshold: int) -> bool:
    """Return True when an item should be reordered.

    An item is reordered once its quantity falls to the threshold or below.
    """
    return quantity < threshold


def restock_list(items: dict[str, int], threshold: int) -> list[str]:
    """Names of the items that need restocking, sorted alphabetically."""
    return sorted(
        name for name, quantity in items.items() if needs_restock(quantity, threshold)
    )
