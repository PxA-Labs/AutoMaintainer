"""Retrieval settings (#526): the engine fields and the runtime's env reads.

``rag_retrieval_enabled`` (RAG_RETRIEVAL_ENABLED) is off by default and
accepts exactly the runtime's flag spellings; ``rag_top_k`` (RAG_TOP_K)
defaults to 8 and is bounded by what match_code_chunks returns (1..50).
Without an engine the runtime reads the same variables, and like its other
settings logs a malformed value and keeps the default.
"""

import logging

import pytest
from pydantic import ValidationError

from automaintainer import AutoMaintainerConfig
from automaintainer._runtime import retrieval

GROQ = "gsk_test_key"


def make(**overrides):
    return AutoMaintainerConfig(groq_api_keys=[GROQ], **overrides)


def test_retrieval_is_off_by_default_with_eight_chunks():
    config = make()
    assert config.rag_retrieval_enabled is False
    assert config.rag_top_k == 8 == retrieval.DEFAULT_TOP_K


def test_from_env_reads_the_retrieval_variables():
    config = AutoMaintainerConfig.from_env(
        {"GROQ_API_KEY": GROQ, "RAG_RETRIEVAL_ENABLED": " On ", "RAG_TOP_K": "12"}
    )
    assert config.rag_retrieval_enabled is True
    assert config.rag_top_k == 12


@pytest.mark.parametrize("raw", ["f", "y", "enabled", "2"])
def test_from_env_refuses_other_flag_spellings(raw):
    with pytest.raises(ValidationError) as info:
        AutoMaintainerConfig.from_env(
            {"GROQ_API_KEY": GROQ, "RAG_RETRIEVAL_ENABLED": raw}
        )
    assert "rag_retrieval_enabled" in str(info.value)


@pytest.mark.parametrize("value", [0, -1, 51, 1000])
def test_top_k_is_bounded(value):
    with pytest.raises(ValidationError):
        make(rag_top_k=value)


@pytest.mark.parametrize("value", [1, 50])
def test_top_k_bounds_are_inclusive(value):
    assert make(rag_top_k=value).rag_top_k == value


# --- The runtime without an engine ---------------------------------------------------


@pytest.mark.parametrize("raw", ["1", "true", "YES", " on "])
def test_runtime_flag_on(monkeypatch, raw):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", raw)
    assert retrieval.retrieval_enabled_from_env() is True


@pytest.mark.parametrize("raw", [None, "", "0", "false", "off"])
def test_runtime_flag_off(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("RAG_RETRIEVAL_ENABLED", raising=False)
    else:
        monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", raw)
    assert retrieval.retrieval_enabled_from_env() is False


def test_runtime_unknown_flag_is_logged_and_off(monkeypatch, caplog):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "enabled")
    with caplog.at_level(logging.WARNING):
        assert retrieval.retrieval_enabled_from_env() is False
    assert "RAG_RETRIEVAL_ENABLED" in caplog.text


@pytest.mark.parametrize("raw, expected", [(None, 8), ("3", 3), ("50", 50)])
def test_runtime_top_k(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("RAG_TOP_K", raising=False)
    else:
        monkeypatch.setenv("RAG_TOP_K", raw)
    assert retrieval.top_k_from_env() == expected


@pytest.mark.parametrize("raw", ["0", "51", "-2", "eight"])
def test_runtime_malformed_top_k_keeps_the_default(monkeypatch, caplog, raw):
    monkeypatch.setenv("RAG_TOP_K", raw)
    with caplog.at_level(logging.WARNING):
        assert retrieval.top_k_from_env() == 8
    assert "RAG_TOP_K" in caplog.text
