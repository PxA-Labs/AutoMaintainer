import pytest
import os
from unittest.mock import MagicMock
from agents import get_all_groq_keys, should_implement, should_iterate, broadcast_log


def test_get_all_groq_keys(monkeypatch):
    # Test with GROQ_API_KEY and additional numbered keys
    monkeypatch.setenv("GROQ_API_KEY", "primary-key")
    monkeypatch.setenv("GROQ_API_KEY_1", "key-1")
    monkeypatch.setenv("GROQ_API_KEY_2", "key-2")
    monkeypatch.delenv("GROQ_API_KEY_3", raising=False)

    keys = get_all_groq_keys()
    assert "primary-key" in keys
    assert "key-1" in keys
    assert "key-2" in keys
    assert len(keys) >= 3


def test_should_implement():
    # PM Decision APPROVED -> implementer
    state_approved = {"pm_decision": "  APPROVED with fixes "}
    assert should_implement(state_approved) == "implementer"

    # PM Decision REJECTED -> END
    state_rejected = {"pm_decision": "REJECTED"}
    assert should_implement(state_rejected) == "__end__"


def test_should_iterate():
    # LGTM -> END
    state_lgtm = {"review": "Looks good to me. LGTM!", "iteration": 0}
    assert should_iterate(state_lgtm) == "__end__"

    # No LGTM, iteration < 3 -> implementer
    state_continue = {"review": "Need more changes", "iteration": 1}
    assert should_iterate(state_continue) == "implementer"

    # No LGTM, iteration >= 3 -> END
    state_limit = {"review": "Still need changes", "iteration": 3}
    assert should_iterate(state_limit) == "__end__"


@pytest.mark.asyncio
async def test_broadcast_log_no_supabase():
    # If supabase is None, broadcast_log should gracefully fallback and not raise error
    import agents

    agents.supabase = None

    # This should run without raising any Exception
    await broadcast_log({"msg": "Test fallback logging", "type": "message"})
