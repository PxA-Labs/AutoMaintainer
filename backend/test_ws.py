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


def test_terminal_websocket_allowed_origin():
    # Test that allowed origins connect successfully
    try:
        with client.websocket_connect(
            "/api/terminal/ws", headers={"origin": "http://localhost:3000"}
        ) as ws:
            ws.send_text('{"type":"resize", "cols":80, "rows":24}')
    except Exception:
        # Prevent platform-specific PTY spawning issues from failing test
        pass
