import pytest

from apiclient.fetch import ApiError, fetch_json


def test_ok_response_is_decoded():
    assert fetch_json(lambda url: (200, '{"a": [1, 2]}'), "/x") == {"a": [1, 2]}


def test_error_status_is_an_api_error():
    with pytest.raises(ApiError):
        fetch_json(lambda url: (500, "oops"), "/x")
