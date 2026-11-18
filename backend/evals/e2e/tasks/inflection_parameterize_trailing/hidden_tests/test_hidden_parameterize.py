import pytest

import inflection


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Hello, World!", "hello-world"),
        ("...and more", "and-more"),
        ("(draft) notes", "draft-notes"),
        ("ends with a dot.", "ends-with-a-dot"),
    ],
)
def test_no_edge_separators(text, expected):
    assert inflection.parameterize(text) == expected


def test_custom_separator():
    assert inflection.parameterize("  (draft) notes", separator="_") == "draft_notes"
    assert inflection.parameterize("a ;; b!", separator="_") == "a_b"
