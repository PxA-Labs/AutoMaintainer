import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import github_app

SECRET = "test-webhook-secret"


def _signature(body: bytes) -> str:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _supabase(existing=None):
    client = MagicMock()
    installations = MagicMock()
    installation_query = (
        installations.select.return_value.eq.return_value.single.return_value
    )
    installation_query.execute.return_value = SimpleNamespace(data={"org_id": "org-1"})

    events = MagicMock()
    duplicate_query = events.select.return_value.eq.return_value.limit.return_value
    duplicate_query.execute.return_value = SimpleNamespace(data=existing or [])
    events.insert.return_value.execute.return_value = SimpleNamespace(data=[])

    def table(name):
        return installations if name == "github_installations" else events

    client.table.side_effect = table
    return client, events


@pytest.mark.asyncio
async def test_valid_webhook_is_queued_and_uses_raw_body():
    body = json.dumps({"action": "opened", "installation": {"id": 7}}).encode()
    supabase, events = _supabase()
    headers = {
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-1",
        "X-Hub-Signature-256": _signature(body),
    }

    with patch.object(github_app, "GITHUB_WEBHOOK_SECRET", SECRET):
        result = await github_app.handle_github_webhook(body, headers, supabase)

    assert result == {
        "status": "queued",
        "event_type": "pull_request",
        "delivery_id": "delivery-1",
    }
    payload = events.insert.call_args.args[0]
    assert payload["payload"] == json.loads(body)
    assert payload["github_delivery_id"] == "delivery-1"
    events.insert.return_value.execute.assert_called_once_with()


@pytest.mark.asyncio
async def test_invalid_signature_is_rejected():
    body = b'{"installation":{"id":7}}'
    headers = {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "delivery-2",
        "X-Hub-Signature-256": "sha256=invalid",
    }

    with patch.object(github_app, "GITHUB_WEBHOOK_SECRET", SECRET):
        with pytest.raises(ValueError, match="Invalid webhook signature"):
            await github_app.handle_github_webhook(body, headers, _supabase()[0])


@pytest.mark.asyncio
async def test_duplicate_delivery_is_not_inserted_again():
    body = b'{"installation":{"id":7}}'
    supabase, events = _supabase(existing=[{"id": "event-1", "status": "pending"}])
    headers = {
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": "delivery-3",
        "X-Hub-Signature-256": _signature(body),
    }

    with patch.object(github_app, "GITHUB_WEBHOOK_SECRET", SECRET):
        result = await github_app.handle_github_webhook(body, headers, supabase)

    assert result["status"] == "duplicate"
    events.insert.assert_not_called()


@pytest.mark.asyncio
async def test_missing_delivery_headers_are_rejected():
    body = b'{"installation":{"id":7}}'
    headers = {"X-Hub-Signature-256": _signature(body)}

    with patch.object(github_app, "GITHUB_WEBHOOK_SECRET", SECRET):
        with pytest.raises(ValueError, match="Missing required GitHub webhook headers"):
            await github_app.handle_github_webhook(body, headers, _supabase()[0])
