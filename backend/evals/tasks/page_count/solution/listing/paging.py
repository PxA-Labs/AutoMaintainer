"""Pagination helpers for list views."""


def page_count(total_items: int, per_page: int) -> int:
    """Number of pages needed to show ``total_items`` with ``per_page`` each."""
    if per_page < 1:
        raise ValueError("per_page must be at least 1")
    return -(-total_items // per_page)


def page_bounds(page: int, per_page: int) -> tuple[int, int]:
    """Start (inclusive) and end (exclusive) item index of a 1-based page."""
    start = (page - 1) * per_page
    return start, start + per_page
