"""Plain-text summary reports."""

from datetime import datetime


def render_report(
    title: str, rows: list[tuple[str, int]], *, now: datetime | None = None
) -> str:
    """Return a text report with a header, one line per row and a total.

    ``now`` is the generation time shown in the header (default: the
    current time).
    """
    if now is None:
        now = datetime.now()
    lines = [title, f"Generated {now:%Y-%m-%d %H:%M}", ""]
    for label, count in rows:
        lines.append(f"{label:<20}{count:>6}")
    lines.append(f"{'Total':<20}{sum(count for _, count in rows):>6}")
    return "\n".join(lines)
