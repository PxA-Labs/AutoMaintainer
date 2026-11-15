"""Helpers for building URL slugs."""

import re


def make_slug(title: str) -> str:
    """Turn a title into a lowercase, URL-friendly slug.

    Letters and digits are kept; every run of other characters becomes a
    single hyphen, and the slug never starts or ends with a hyphen.
    """
    lowered = title.lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")
