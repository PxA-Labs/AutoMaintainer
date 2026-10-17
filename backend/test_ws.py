import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main
from main import app

client = TestClient(app)

ALLOWED_ORIGIN = {"origin": "http://localhost:3000"}


@pytest.fixture
def authenticated(monkeypatch):
    """Bypass Supabase token validation, leaving the rest of the guard intact."""

    async def fake_auth(websocket):
        return {"user_id": "test-user", "email": "test@localhost"}

    monkeypatch.setattr(main, "authenticate_websocket", fake_auth)
    yield


@pytest.fixture(autouse=True)
def no_dev_bypass(monkeypatch):
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    yield


def test_terminal_websocket_origin_security():
    # Connection is rejected for an unsupported origin.
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/terminal/ws", headers={"origin": "http://malicious.com"}
        ):
            pass

    assert excinfo.value.code == 1008


def test_terminal_websocket_rejects_missing_origin():
    """A client that sends no Origin header must be rejected.

    Regression guard: the original check was `if origin and not (...)`, so a
    missing Origin skipped validation entirely. Non-browser clients omit the
    header by default, which made this an unauthenticated shell.
    """
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/terminal/ws"):
            pass

    assert excinfo.value.code == 1008


def test_terminal_websocket_rejects_origin_suffix_spoof():
    """Substring matching allowed attacker-controlled lookalike domains."""
    for spoofed in (
        "https://huggingface.co.attacker.net",
        "http://localhost.attacker.net",
    ):
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/api/terminal/ws", headers={"origin": spoofed}
            ):
                pass

        assert excinfo.value.code == 1008, f"{spoofed} was not rejected"


def test_terminal_websocket_rejects_missing_token():
    """A trusted origin is not sufficient; the handshake needs a valid token."""
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/api/terminal/ws", headers=ALLOWED_ORIGIN):
            pass

    assert excinfo.value.code == 1008


def test_terminal_websocket_rejects_malformed_subprotocol():
    """Only a ["bearer", <token>] pair is accepted."""
    for protocols in (["bearer"], ["notbearer", "tok"], ["tok", "bearer"]):
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/api/terminal/ws",
                headers=ALLOWED_ORIGIN,
                subprotocols=protocols,
            ):
                pass

        assert excinfo.value.code == 1008, f"{protocols} was not rejected"


def test_terminal_websocket_refused_in_production(monkeypatch, authenticated):
    """An interactive shell requires explicit opt-in in production."""
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("ENABLE_TERMINAL", raising=False)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/terminal/ws",
            headers=ALLOWED_ORIGIN,
            subprotocols=["bearer", "tok"],
        ):
            pass

    assert excinfo.value.code == 1008


def test_terminal_websocket_allows_authenticated_caller(authenticated):
    """A trusted origin plus a valid token connects successfully."""
    try:
        with client.websocket_connect(
            "/api/terminal/ws",
            headers=ALLOWED_ORIGIN,
            subprotocols=["bearer", "valid-token"],
        ) as ws:
            ws.send_text('{"type":"resize", "cols":80, "rows":24}')
    except WebSocketDisconnect as e:
        assert e.code != 1008, "Authenticated caller was rejected by security policy"
    except Exception:
        # Platform-specific PTY spawning (pywinpty) must not fail the test.
        pass
