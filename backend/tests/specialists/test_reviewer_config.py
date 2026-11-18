"""``reviewer_backend`` in AutoMaintainerConfig (#525)."""

import pytest
from pydantic import ValidationError

from automaintainer import config as config_module
from automaintainer.config import ENV_VARS, AutoMaintainerConfig

KEYS = {"groq_api_keys": ["k"]}


def test_default_is_langgraph():
    assert AutoMaintainerConfig(**KEYS).reviewer_backend == "langgraph"


def test_adk_is_accepted_when_the_extra_is_installed(monkeypatch):
    monkeypatch.setattr(config_module, "_adk_missing", lambda: None)
    config = AutoMaintainerConfig(**KEYS, reviewer_backend="adk")
    assert config.reviewer_backend == "adk"


def test_unknown_backend_is_rejected():
    with pytest.raises(ValidationError) as raised:
        AutoMaintainerConfig(**KEYS, reviewer_backend="crew")
    assert "reviewer_backend" in str(raised.value)


def test_adk_without_the_extra_fails_validation_never_falls_back(monkeypatch):
    monkeypatch.setattr(config_module, "_adk_missing", lambda: "google.adk")
    with pytest.raises(ValidationError) as raised:
        AutoMaintainerConfig(**KEYS, reviewer_backend="adk")
    message = str(raised.value)
    assert "reviewer_backend" in message
    assert "pip install 'automaintainer[adk]'" in message
    assert "google.adk" in message


def test_from_env_reads_reviewer_backend(monkeypatch):
    monkeypatch.setattr(config_module, "_adk_missing", lambda: None)
    assert ENV_VARS["reviewer_backend"] == "REVIEWER_BACKEND"
    env = {"GROQ_API_KEY": "k", "REVIEWER_BACKEND": " ADK "}
    assert AutoMaintainerConfig.from_env(env).reviewer_backend == "adk"
    env["REVIEWER_BACKEND"] = ""
    assert AutoMaintainerConfig.from_env(env).reviewer_backend == "langgraph"


def test_adk_missing_reports_a_missing_module(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec

    def find_spec(name, *a):
        if name == "google.adk":
            raise ModuleNotFoundError("No module named 'google'")
        return real(name, *a)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert config_module._adk_missing() == "google.adk"
