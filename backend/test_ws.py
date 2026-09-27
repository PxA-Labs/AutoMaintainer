import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main
from main import app

client = TestClient(app)


def test_terminal_websocket_origin_security():
    # Origin rejection happens before the authentication handshake.
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/terminal/ws", headers={"origin": "http://malicious.com"}
        ):
            pass

    assert excinfo.value.code == 1008


def test_terminal_websocket_requires_authentication():
    with client.websocket_connect(
        "/api/terminal/ws?repo_url=PxA-Labs/AutoMaintainer",
        headers={"origin": "http://localhost:3000"},
    ) as websocket:
        websocket.send_text(json.dumps({"type": "resize", "cols": 80, "rows": 24}))
        with pytest.raises(WebSocketDisconnect) as excinfo:
            websocket.receive_text()

    assert excinfo.value.code == 1008


def test_terminal_websocket_forwards_handshake_bearer_token(monkeypatch):
    received = {}

    async def fake_authorize(websocket, repo_url, authorization):
        received.update(
            repo_url=repo_url,
            authorization=authorization,
        )
        await websocket.close(code=1000)
        return None

    monkeypatch.setattr(main, "authorize_terminal", fake_authorize)

    with client.websocket_connect(
        "/api/terminal/ws?repo_url=https://github.com/PxA-Labs/AutoMaintainer.git",
        headers={"origin": "http://localhost:3000"},
    ) as websocket:
        websocket.send_text(json.dumps({"type": "auth", "access_token": "token-123"}))
        with pytest.raises(WebSocketDisconnect) as excinfo:
            websocket.receive_text()

    assert excinfo.value.code == 1000
    assert received == {
        "repo_url": "https://github.com/PxA-Labs/AutoMaintainer.git",
        "authorization": "Bearer token-123",
    }
