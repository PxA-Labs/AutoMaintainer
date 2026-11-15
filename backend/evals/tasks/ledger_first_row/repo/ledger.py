"""Tiny CSV ledger utilities."""

import csv
import io


def parse_rows(text: str) -> list[dict[str, str]]:
    """Parse CSV text with a ``category,amount`` header into row dicts."""
    return list(csv.DictReader(io.StringIO(text)))


def total_by_category(rows: list[dict[str, str]]) -> dict[str, float]:
    """Sum the ``amount`` of every row per ``category``."""
    totals: dict[str, float] = {}
    for row in rows[1:]:  # skip the header
        category = row["category"]
        totals[category] = totals.get(category, 0.0) + float(row["amount"])
    return totals
