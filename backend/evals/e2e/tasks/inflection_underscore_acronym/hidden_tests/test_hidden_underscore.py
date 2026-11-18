import pytest

import inflection


@pytest.mark.parametrize(
    "word, expected",
    [
        ("HTTPServer", "http_server"),
        ("IOError", "io_error"),
        ("XMLHttpRequest", "xml_http_request"),
        ("getHTTPResponseCode", "get_http_response_code"),
    ],
)
def test_acronym_boundary(word, expected):
    assert inflection.underscore(word) == expected


def test_tableize_uses_the_same_boundaries():
    assert inflection.tableize("XMLHttpRequest") == "xml_http_requests"
    assert inflection.tableize("RawScaledScorer") == "raw_scaled_scorers"
