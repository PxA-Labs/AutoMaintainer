"""#190: POST /webhooks/github rejects a missing or wrong signature with 401.

The signature is checked before the body is parsed or the database is read;
a correctly signed but malformed delivery stays 400.
"""

import hashlib
import hmac
import json
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

import github_app

SECRET = "test-webhook-secret"


def _post(monkeypatch, body: bytes, signature=None, **headers):
    import main

    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(github_app, "GITHUB_WEBHOOK_SECRET", SECRET)
    if signature is None:
        digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        signature = f"sha256={digest}"
    sent = {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "delivery-1"}
    if signature is not False:
        sent["X-Hub-Signature-256"] = signature
    sent.update(headers)
    return TestClient(main.app).post("/webhooks/github", content=body, headers=sent)


@pytest.mark.parametrize(
    "signature",
    [
        "sha256=" + "0" * 64,
        "sha256=invalid",
        "sha1=abc",
        "",
        False,  # header missing
        # Non-ASCII bytes: hmac.compare_digest would raise TypeError on them.
        "sha256=éé".encode("utf-8"),
    ],
    ids=["wrong", "garbage", "wrong-algo", "empty", "missing", "non-ascii"],
)
def test_bad_signature_is_401_before_parsing_or_db(monkeypatch, signature):
    import agents

    db = MagicMock()
    monkeypatch.setattr(agents, "supabase", db)

    response = _post(monkeypatch, b"not even json", signature=signature)

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid webhook signature"}
    db.table.assert_not_called()


def test_signed_but_malformed_payload_stays_400(monkeypatch):
    import agents

    monkeypatch.setattr(agents, "supabase", MagicMock())

    response = _post(monkeypatch, b"not json")

    assert response.status_code == 400


def test_signed_delivery_without_delivery_id_stays_400(monkeypatch):
    import agents

    monkeypatch.setattr(agents, "supabase", MagicMock())
    body = json.dumps({"installation": {"id": 7}}).encode()

    response = _post(monkeypatch, body, **{"X-GitHub-Delivery": ""})

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_signature_error_is_typed(monkeypatch):
    monkeypatch.setattr(github_app, "GITHUB_WEBHOOK_SECRET", SECRET)

    with pytest.raises(github_app.WebhookSignatureError):
        await github_app.handle_github_webhook(
            b"{}", {"X-Hub-Signature-256": "sha256=bad"}, MagicMock()
        )
    # Callers that catch ValueError keep working.
    assert issubclass(github_app.WebhookSignatureError, ValueError)
