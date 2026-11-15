"""Small matrix helpers for the spreadsheet export."""


def transpose(rows: list[list]) -> list[list]:
    """Return the transpose of a rectangular matrix given as a list of rows."""
    if not rows:
        return []
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        raise ValueError("all rows must have the same length")
    return [[row[i] for row in rows] for i in range(width)]
