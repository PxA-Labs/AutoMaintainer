"""Repository-wide pytest configuration for the backend."""

import pytest


@pytest.fixture(autouse=True)
def _pin_github_auth_mode(monkeypatch):
    """Run every test in single-tenant PAT mode unless it opts in to another.

    github_app.get_auth_mode() defaults to ``app`` whenever GitHub App
    credentials are configured, and github_app loads backend/.env itself. A
    developer whose .env holds real App credentials would otherwise see agent
    tests fail or pass depending on their machine. Tests that exercise App
    mode set or clear GITHUB_AUTH_MODE themselves (see test_tenant_tokens.py),
    which overrides this default for that test.
    """
    monkeypatch.setenv("GITHUB_AUTH_MODE", "pat")
