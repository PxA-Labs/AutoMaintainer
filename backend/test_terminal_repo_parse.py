"""Terminal repo_url parsing: accepted formats and linear-time behaviour."""

import time

import pytest

from main import _parse_terminal_repo


@pytest.mark.parametrize(
    "value,expected",
    [
        ("owner/repo", "owner/repo"),
        ("owner/repo.git", "owner/repo"),
        ("owner/repo/", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://www.github.com/owner/repo/", "owner/repo"),
        ("http://github.com/my-org/my.repo_1", "my-org/my.repo_1"),
    ],
)
def test_accepts_slug_and_github_urls(value, expected):
    assert _parse_terminal_repo(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "",
        "owner",
        "owner/repo/extra",
        "https://gitlab.com/owner/repo",
        "../repo",
        "owner/..",
        "./repo",
        "owner/re po",
        "owner/repo\n",
        "a" * 300 + "/b",
    ],
)
def test_rejects_invalid_values(value):
    assert _parse_terminal_repo(value) is None


def test_pathological_input_is_fast():
    # CodeQL flagged the previous regex as polynomial on runs of '-'.
    value = "a/" + "-" * 250 + "!"
    start = time.perf_counter()
    assert _parse_terminal_repo(value) is None
    assert time.perf_counter() - start < 0.05
