import json

import pytest

from apiclient.fetch import (
    ApiError,
    BadResponse,
    ClientError,
    NotFound,
    ServerError,
    fetch_json,
)


class Script:
    """A transport that replays a list of responses and counts calls."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, url):
        self.calls += 1
        return self.responses.pop(0)


def test_any_2xx_is_success():
    assert fetch_json(Script((201, '{"id": 7}')), "/x") == {"id": 7}
    assert fetch_json(Script((204, "null")), "/x") is None


def test_404_is_not_found_and_not_retried():
    transport = Script((404, ""), (200, "{}"))
    with pytest.raises(NotFound) as info:
        fetch_json(transport, "/missing", retries=3)
    assert isinstance(info.value, ApiError) and transport.calls == 1


@pytest.mark.parametrize("status", [400, 401, 422, 499])
def test_other_4xx_is_client_error(status):
    transport = Script((status, ""), (200, "{}"))
    with pytest.raises(ClientError) as info:
        fetch_json(transport, "/x", retries=2)
    assert info.value.status == status and transport.calls == 1


def test_5xx_is_retried_then_succeeds():
    transport = Script((503, ""), (502, ""), (200, "[1]"))
    assert fetch_json(transport, "/x", retries=2) == [1]
    assert transport.calls == 3


def test_5xx_exhausts_retries():
    transport = Script((500, ""), (503, ""), (504, ""), (200, "{}"))
    with pytest.raises(ServerError) as info:
        fetch_json(transport, "/x", retries=2)
    assert info.value.status == 504 and transport.calls == 3


def test_5xx_without_retries():
    transport = Script((500, ""))
    with pytest.raises(ServerError):
        fetch_json(transport, "/x")
    assert transport.calls == 1


def test_invalid_json_is_bad_response_chained():
    with pytest.raises(BadResponse) as info:
        fetch_json(Script((200, "<html>")), "/x")
    assert isinstance(info.value.__cause__, json.JSONDecodeError)


def test_unexpected_status_is_plain_api_error():
    with pytest.raises(ApiError) as info:
        fetch_json(Script((302, "")), "/x")
    assert type(info.value) is ApiError
