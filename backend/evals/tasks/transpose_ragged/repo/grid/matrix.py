"""Small matrix helpers for the spreadsheet export."""


def transpose(rows: list[list]) -> list[list]:
    """Return the transpose of a rectangular matrix given as a list of rows."""
    return list(zip(*rows))
