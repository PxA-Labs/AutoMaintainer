"""Installation token minting (epic #412): suspension checks before a token
exists, and tokens narrowed to one repository and the pipeline's permissions.

No real GitHub: App-JWT calls go through httpx.MockTransport, and the
installation row lookup uses an in-memory double.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

import github_app


class FakeGitHub:
    """GET /app/installations/{id} and POST .../access_tokens."""

    def __init__(self, *, suspended_at=None, permissions=None, account="acme"):
        self.installation = {
            "id": 7,
            "account": {"login": account, "type": "Organization"},
            "suspended_at": suspended_at,
            "permissions": (
                dict(github_app.RUN_TOKEN_PERMISSIONS)
                if permissions is None
                else permissions
            ),
        }
        self.get_status = 200
        self.post_response = None
        self.requests = []
        self.minted = 0

    def handler(self, request: httpx.Request):
        assert request.headers["authorization"] == "Bearer app-jwt"
        self.requests.append(request)
        if request.method == "GET":
            if self.get_status != 200:
                return httpx.Response(self.get_status, json={"message": "Not Found"})
            return httpx.Response(200, json=self.installation)
        if self.post_response is not None:
            return self.post_response
        self.minted += 1
        return httpx.Response(
            201,
            json={"token": f"tok-{self.minted}", "expires_at": "2999-01-01T00:00:00Z"},
        )

    @property
    def posts(self):
        return [r for r in self.requests if r.method == "POST"]

    def body(self, i=-1):
        import json

        return json.loads(self.posts[i].content)


@pytest.fixture
def gh(monkeypatch):
    fake = FakeGitHub()
    transport = httpx.MockTransport(fake.handler)
    monkeypatch.setattr(github_app, "generate_app_jwt", lambda: "app-jwt")
    monkeypatch.setattr(
        github_app,
        "_app_api_client",
        lambda: httpx.Client(base_url=github_app.GITHUB_API_URL, transport=transport),
    )
    monkeypatch.setattr(github_app, "_installation_status_client", lambda: None)
    github_app._installation_token_cache.clear()
    yield fake
    github_app._installation_token_cache.clear()


def installation_rows(monkeypatch, rows=None, error=None):
    """The service-role client sees these github_installations rows."""

    def execute():
        if error:
            raise error
        return SimpleNamespace(data=[dict(r) for r in rows or []])

    query = MagicMock()
    query.select.return_value = query
    query.eq.return_value = query
    query.limit.return_value = query
    query.execute.side_effect = execute
    sb = MagicMock()
    sb.table.return_value = query
    monkeypatch.setattr(github_app, "_installation_status_client", lambda: sb)


# --- suspension ---------------------------------------------------------------


def test_suspended_on_github_is_refused_before_minting_and_not_cached(gh):
    gh.installation["suspended_at"] = "2026-11-01T00:00:00Z"

    for _ in range(2):  # the refusal is re-checked, never cached
        with pytest.raises(github_app.InstallationSuspendedError, match="suspended"):
            github_app.get_installation_token_sync(7, repository="acme/app")

    assert gh.posts == []
    assert len(gh.requests) == 2
    assert github_app._installation_token_cache == {}


def test_suspended_installation_row_is_refused_without_calling_github(gh, monkeypatch):
    installation_rows(monkeypatch, [{"suspended_at": "2026-11-01T00:00:00Z"}])

    with pytest.raises(github_app.InstallationSuspendedError):
        github_app.get_installation_token_sync(7)

    assert gh.requests == []
    assert github_app._installation_token_cache == {}


def test_active_installation_row_still_checks_github(gh, monkeypatch):
    installation_rows(monkeypatch, [{"suspended_at": None}])
    assert github_app.get_installation_token_sync(7) == "tok-1"


def test_database_error_falls_back_to_githubs_suspension_check(gh, monkeypatch):
    installation_rows(monkeypatch, error=RuntimeError("db down"))
    gh.installation["suspended_at"] = "2026-11-01T00:00:00Z"
    with pytest.raises(github_app.InstallationSuspendedError):
        github_app.get_installation_token_sync(7)


def test_uninstalled_installation_is_refused(gh):
    gh.get_status = 404
    with pytest.raises(github_app.InstallationSuspendedError, match="uninstalled"):
        github_app.get_installation_token_sync(7)
    assert gh.posts == []


def test_suspension_reported_when_minting_is_refused(gh):
    gh.post_response = httpx.Response(
        403, json={"message": "This installation has been suspended"}
    )
    with pytest.raises(github_app.InstallationSuspendedError):
        github_app.get_installation_token_sync(7)
    assert github_app._installation_token_cache == {}


def test_suspended_error_is_a_github_auth_error():
    """Callers that refuse runs on GitHubAuthError refuse suspended ones too."""
    assert issubclass(github_app.InstallationSuspendedError, github_app.GitHubAuthError)


def test_invalidate_drops_every_cached_scope_of_the_installation(gh):
    github_app.get_installation_token_sync(7, repository="acme/a")
    github_app.get_installation_token_sync(7, repository="acme/b")
    github_app.get_installation_token_sync(
        8, permissions=github_app.SYNC_TOKEN_PERMISSIONS
    )

    github_app.invalidate_installation_tokens(7)

    assert [k[0] for k in github_app._installation_token_cache] == [8]


# --- minimum-permission tokens ------------------------------------------------


def test_run_token_is_scoped_to_the_repository_and_pipeline_permissions(gh):
    assert github_app.get_installation_token_sync(7, repository="acme/app") == "tok-1"

    assert gh.posts[0].url.path == "/app/installations/7/access_tokens"
    assert gh.body() == {
        "repositories": ["app"],
        "permissions": {
            "contents": "write",
            "pull_requests": "write",
            "issues": "write",
            "statuses": "write",
            "metadata": "read",
        },
    }


def test_checks_read_is_requested_only_when_granted(gh):
    github_app.get_installation_token_sync(7)
    assert "checks" not in gh.body()["permissions"]

    github_app._installation_token_cache.clear()
    gh.installation["permissions"]["checks"] = "write"
    github_app.get_installation_token_sync(7)
    assert gh.body()["permissions"]["checks"] == "read"


def test_missing_required_permission_is_a_clear_error(gh):
    gh.installation["permissions"] = {"contents": "read", "metadata": "read"}

    with pytest.raises(github_app.GitHubAuthError) as exc:
        github_app.get_installation_token_sync(7)

    message = str(exc.value)
    assert "contents:write" in message and "pull_requests:write" in message
    assert gh.posts == []


def test_repository_of_another_account_is_refused(gh):
    with pytest.raises(github_app.GitHubAuthError, match="does not belong"):
        github_app.get_installation_token_sync(7, repository="victim/app")
    assert gh.posts == []


def test_each_repository_scope_is_cached_separately(gh):
    a = github_app.get_installation_token_sync(7, repository="acme/a")
    assert github_app.get_installation_token_sync(7, repository="ACME/A") == a
    b = github_app.get_installation_token_sync(7, repository="acme/b")
    assert a != b
    assert [p["repositories"] for p in map(gh.body, range(2))] == [["a"], ["b"]]


def test_sync_token_requests_metadata_read_only(gh):
    github_app.get_installation_token_sync(
        7, permissions=github_app.SYNC_TOKEN_PERMISSIONS
    )
    assert gh.body() == {"permissions": {"metadata": "read"}}


@pytest.mark.asyncio
async def test_repository_listing_uses_a_metadata_only_token(monkeypatch):
    seen = {}

    async def token(installation_id, **scope):
        seen.update(scope, installation_id=installation_id)
        return "t"

    monkeypatch.setattr(github_app, "get_installation_token", token)
    fake_client = MagicMock()
    fake_client.get_installation_repos.return_value = []
    monkeypatch.setattr(github_app, "Github", lambda **_k: fake_client)

    assert await github_app.get_installation_repositories(7) == []
    assert seen == {"installation_id": 7, "permissions": {"metadata": "read"}}


def test_credentials_pass_the_repository_scope_to_minting(monkeypatch):
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    monkeypatch.setattr(github_app, "GITHUB_APP_ID", "1")
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "pem")
    calls = []
    monkeypatch.setattr(
        github_app,
        "get_installation_token_sync",
        lambda iid, **scope: calls.append((iid, scope)) or "t",
    )

    creds = github_app.credentials_for_installation(
        7, purpose="run r1", repository="acme/app"
    )

    assert creds.token() == "t"
    assert calls == [(7, {"repository": "acme/app"})]
