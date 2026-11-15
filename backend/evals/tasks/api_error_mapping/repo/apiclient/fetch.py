"""A small JSON-over-HTTP client with a pluggable transport."""

import json


class ApiError(Exception):
    """Base class of every error raised by this module."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class NotFound(ApiError):
    """The resource does not exist (HTTP 404)."""


class ClientError(ApiError):
    """The request was rejected (HTTP 4xx other than 404)."""


class ServerError(ApiError):
    """The server failed (HTTP 5xx), after any retries."""


class BadResponse(ApiError):
    """A successful response whose body is not valid JSON."""


def fetch_json(transport, url: str, retries: int = 0):
    """Return the decoded JSON body of ``transport(url)``."""
    status, body = transport(url)
    if status != 200:
        raise ApiError(f"{url}: HTTP {status}", status)
    return json.loads(body)
