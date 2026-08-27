import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from main import app

client = TestClient(app)


def test_terminal_websocket_origin_security():
    # Test that connection is closed/rejected for unsupported origin
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/terminal/ws", headers={"origin": "http://malicious.com"}
        ):
            pass

    assert excinfo.value.code == 1008


def test_terminal_websocket_requires_authentication():
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/terminal/ws?repo_url=PxA-Labs/AutoMaintainer",
            headers={"origin": "http://localhost:3000"},
        ):
            pass

    assert excinfo.value.code == 1008
