import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from main import app, require_repository_access

client = TestClient(app)


def _route(path):
    return next(route for route in app.routes if getattr(route, "path", None) == path)


def test_sensitive_repository_routes_require_repository_access():
    protected_paths = {
        "/start-legacy",
        "/repo/{repo_name:path}/file",
        "/repo/{repo_name:path}/file/create",
        "/repo/{repo_name:path}/tree",
        "/repo/{repo_name:path}/search",
    }

    for path in protected_paths:
        route = _route(path)
        assert any(
            dependency.call is require_repository_access
            for dependency in route.dependant.dependencies
        ), path


def test_propose_changes_requires_repository_access():
    route = _route("/repo/{repo_name:path}/propose-changes")
    assert any(
        dependency.call is require_repository_access
        for dependency in route.dependant.dependencies
    )


def test_legacy_stop_requires_authentication():
    route = _route("/stop-legacy")
    assert any(
        dependency.call.__name__ == "get_current_user"
        for dependency in route.dependant.dependencies
    )


def test_terminal_rejects_missing_authentication():
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/terminal/ws?repo_url=PxA-Labs/AutoMaintainer",
        ):
            pass

    assert excinfo.value.code == 1008
