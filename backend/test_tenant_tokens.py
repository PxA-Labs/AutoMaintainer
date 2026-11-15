"""Per-tenant GitHub credentials (epic #412): installation tokens per
repository, the explicit PAT fallback, verified installation claims, and
repository sync never moving a repository between orgs.

No real GitHub or Supabase: tokens are minted by a fake, user-token calls go
through httpx.MockTransport, and the database is an in-memory double.
"""

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError

import agents
import github_app
import main
import tasks
from events import NullEmitter
from main import app

client = TestClient(app)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("GITHUB_AUTH_MODE", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setattr(github_app, "GITHUB_APP_ID", None)
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", None)
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY_PATH", None)
    github_app._installation_token_cache.clear()
    yield
    github_app._installation_token_cache.clear()
    app.dependency_overrides.clear()


@pytest.fixture
def app_creds(monkeypatch):
    """GitHub App configured; installation N mints token 'inst-N-token'."""
    monkeypatch.setattr(github_app, "GITHUB_APP_ID", "123")
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "pem")
    minted = []

    def mint(installation_id, **_scope):
        minted.append(int(installation_id))
        return f"inst-{int(installation_id)}-token"

    monkeypatch.setattr(github_app, "get_installation_token_sync", mint)
    return minted


class Rows:
    """PostgREST query double over a list of dict rows (eq/in_ filters)."""

    def __init__(self, store, table):
        self.store, self.table = store, table
        self.filters = []
        self.op = ("select", None)

    def select(self, *_a, **_k):
        return self

    def limit(self, *_a):
        return self

    def single(self):
        self.op = ("single", None)
        return self

    def maybe_single(self):
        # Like postgrest-py: execute() returns None (not a response) when no
        # row matches, so callers must handle a None result.
        self.op = ("maybe_single", None)
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def insert(self, row):
        self.op = ("insert", row)
        return self

    def update(self, row):
        self.op = ("update", row)
        return self

    def upsert(self, row, on_conflict=None):
        self.op = ("upsert", row)
        return self

    def execute(self):
        rows = self.store.setdefault(self.table, [])
        kind, row = self.op
        self.store.setdefault("_writes", []).append((self.table, kind, row))
        matched = [r for r in rows if all(f(r) for f in self.filters)]
        if kind == "insert":
            if any(r["id"] == row["id"] for r in rows):
                raise Exception("duplicate key value violates unique constraint")
            rows.append(dict(row))
        elif kind in ("update", "upsert"):
            hits = (
                matched
                if kind == "update"
                else [r for r in rows if r["id"] == row["id"]]
            )
            for r in hits:
                r.update(row)
            if kind == "upsert" and not hits:
                rows.append(dict(row))
        elif kind == "single":
            return SimpleNamespace(data=matched[0] if matched else None)
        elif kind == "maybe_single":
            return SimpleNamespace(data=matched[0]) if matched else None
        return SimpleNamespace(data=matched)


def fake_db(**tables):
    store = {k: [dict(r) for r in v] for k, v in tables.items()}
    sb = MagicMock()
    sb.table.side_effect = lambda name: Rows(store, name)
    return sb, store


def writes(store, table, kind=None):
    return [
        row
        for t, k, row in store.get("_writes", [])
        if t == table
        and k in ("insert", "update", "upsert")
        and (kind is None or k == kind)
    ]


# --- auth mode / PAT fallback rules -----------------------------------------


def test_mode_defaults_to_app_when_app_credentials_are_configured(app_creds):
    assert github_app.get_auth_mode() == "app"


def test_mode_defaults_to_pat_without_app_credentials():
    assert github_app.get_auth_mode() == "pat"


def test_pat_mode_logs_a_startup_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="github_app"):
        github_app._log_auth_mode_at_startup()
    assert "single-tenant" in caplog.text


def test_explicit_mode_overrides_default(monkeypatch, app_creds):
    monkeypatch.setenv("GITHUB_AUTH_MODE", "pat")
    assert github_app.get_auth_mode() == "pat"


def test_unknown_mode_is_rejected_not_guessed(monkeypatch):
    monkeypatch.setenv("GITHUB_AUTH_MODE", "apps")
    with pytest.raises(github_app.GitHubAuthError):
        github_app.get_auth_mode()


def test_app_mode_uses_the_installation_token_even_if_a_pat_is_set(
    monkeypatch, app_creds
):
    monkeypatch.setenv("GITHUB_TOKEN", "global-pat")
    creds = github_app.credentials_for_installation(7, purpose="t")
    assert creds.token() == "inst-7-token"


def test_app_mode_refuses_pat_fallback_and_logs_loudly(monkeypatch, app_creds, caplog):
    monkeypatch.setenv("GITHUB_TOKEN", "global-pat")
    with caplog.at_level(logging.ERROR, logger="github_app"):
        with pytest.raises(github_app.GitHubAuthError):
            github_app.credentials_for_installation(None, purpose="run r1")
    assert "REFUSING" in caplog.text and "run r1" in caplog.text


def test_explicit_app_mode_without_app_credentials_is_refused(monkeypatch):
    monkeypatch.setenv("GITHUB_AUTH_MODE", "app")
    monkeypatch.setenv("GITHUB_TOKEN", "global-pat")
    with pytest.raises(github_app.GitHubAuthError):
        github_app.credentials_for_installation(7, purpose="t")


def test_pat_mode_uses_github_token(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "global-pat")
    creds = github_app.credentials_for_installation(7, purpose="t")
    assert creds.mode == "pat" and creds.token() == "global-pat"
    assert "global-pat" not in repr(creds)


def test_pat_mode_without_github_token_is_an_error():
    with pytest.raises(github_app.GitHubAuthError):
        github_app.credentials_for_installation(7, purpose="t")


# --- token cache (fake clock) ------------------------------------------------


def test_installation_token_is_cached_until_shortly_before_expiry(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(github_app.time, "time", lambda: now[0])
    monkeypatch.setattr(github_app, "GITHUB_APP_ID", "123")
    monkeypatch.setattr(github_app, "generate_app_jwt", lambda: "app-jwt")
    monkeypatch.setattr(github_app, "_installation_status_client", lambda: None)
    issued = []

    def handler(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": 5,
                    "account": {"login": "o"},
                    "suspended_at": None,
                    "permissions": dict(github_app.RUN_TOKEN_PERMISSIONS),
                },
            )
        issued.append(request.url.path)
        expires = datetime.fromtimestamp(now[0] + 3600, tz=timezone.utc)
        return httpx.Response(
            201,
            json={
                "token": f"tok-{len(issued)}",
                "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        github_app,
        "_app_api_client",
        lambda: httpx.Client(base_url=github_app.GITHUB_API_URL, transport=transport),
    )

    assert github_app.get_installation_token_sync(5) == "tok-1"
    now[0] += 3600 - github_app.TOKEN_REFRESH_MARGIN_SECONDS - 1
    assert github_app.get_installation_token_sync(5) == "tok-1"
    now[0] += 2  # now inside the refresh margin
    assert github_app.get_installation_token_sync(5) == "tok-2"
    # Separate installations never share a cached token.
    assert github_app.get_installation_token_sync(6) == "tok-3"


def test_credentials_client_is_rebuilt_after_token_refresh(monkeypatch):
    tokens = iter(["t1", "t1", "t2"])
    monkeypatch.setattr(
        github_app, "get_installation_token_sync", lambda _id, **_k: next(tokens)
    )
    creds = github_app.GitHubCredentials(installation_id=1)
    first = creds.client()
    assert creds.client() is first
    assert creds.client() is not first


# --- token selection per repository: /repo routes ---------------------------


REPO_ROWS = [
    {"id": 1, "org_id": "org-a", "full_name": "a/app", "github_installation_id": 11},
    {"id": 2, "org_id": "org-b", "full_name": "b/app", "github_installation_id": 22},
    {
        "id": 3,
        "org_id": "org-a",
        "full_name": "a/legacy",
        "github_installation_id": None,
    },
]


@pytest.fixture
def repo_routes(monkeypatch, tmp_path):
    """Authenticated member of ``org`` hitting the file API; records tokens."""
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    sb, _ = fake_db(repositories=REPO_ROWS)
    monkeypatch.setattr(agents, "supabase", sb)
    used = []
    repo = MagicMock()
    repo.get_contents.return_value = SimpleNamespace(path="a.py", sha="s")
    repo.default_branch = "main"
    repo.create_pull.return_value = SimpleNamespace(number=1, html_url="https://pr")

    def fake_github(token):
        used.append(token)
        return MagicMock(get_repo=lambda _name: repo)

    monkeypatch.setattr(main, "Github", fake_github)

    def as_member(org):
        async def user():
            return {"user_id": "u", "org_id": org, "role": "member", "email": "e"}

        app.dependency_overrides[main.get_current_user] = user
        app.dependency_overrides[main.require_repo_access] = lambda: org

    return SimpleNamespace(used=used, as_member=as_member)


def test_each_repo_route_uses_its_own_installation_token(
    monkeypatch, app_creds, repo_routes
):
    monkeypatch.setenv("GITHUB_TOKEN", "global-pat")

    repo_routes.as_member("org-a")
    r1 = client.post("/repo/a/app/file", json={"file_path": "a.py", "content": "x"})
    repo_routes.as_member("org-b")
    r2 = client.post("/repo/b/app/file", json={"file_path": "a.py", "content": "x"})

    assert r1.status_code == r2.status_code == 200
    assert repo_routes.used == ["inst-11-token", "inst-22-token"]
    assert app_creds == [11, 22]


def test_repo_route_never_uses_another_orgs_installation(app_creds, repo_routes):
    # org-a asking for org-b's repository finds no row in its own org.
    repo_routes.as_member("org-a")
    r = client.delete("/repo/b/app/file?file_path=a.py")
    assert r.status_code == 404
    assert repo_routes.used == [] and app_creds == []


def test_repo_without_installation_is_refused_in_app_mode(
    monkeypatch, app_creds, repo_routes
):
    monkeypatch.setenv("GITHUB_TOKEN", "global-pat")
    repo_routes.as_member("org-a")
    r = client.post("/repo/a/legacy/file/create", json={"file_path": "b.py"})
    assert r.status_code == 503
    assert repo_routes.used == []


def test_pat_mode_repo_route_uses_github_token(monkeypatch, repo_routes):
    monkeypatch.setenv("GITHUB_TOKEN", "global-pat")
    repo_routes.as_member("org-a")
    r = client.post("/repo/a/app/file", json={"file_path": "a.py", "content": "x"})
    assert r.status_code == 200
    assert repo_routes.used == ["global-pat"]


def test_propose_changes_uses_installation_token(app_creds, repo_routes):
    repo_routes.as_member("org-b")
    r = client.post(
        "/repo/b/app/propose-changes",
        json={"title": "t", "changes": [{"path": "a.py", "content": "x"}]},
    )
    assert r.status_code == 200
    assert repo_routes.used == ["inst-22-token"]


# --- token selection per run: run_agent_loop --------------------------------


class GraphProbe:
    """Stands in for the compiled graph; records what a node would see."""

    def __init__(self, delay=0.0):
        self.seen = []
        self.delay = delay

    async def astream(self, state, stream_mode=None):
        await asyncio.sleep(self.delay)
        self.seen.append(
            (state["repo_name"], bool(agents.gh), agents._run_github_token())
        )
        yield {**state, "log_messages": []}


@pytest.fixture
def probe(monkeypatch):
    monkeypatch.setattr(agents, "supabase", None)
    graph = GraphProbe()
    monkeypatch.setattr(agents, "app", graph)
    return graph


@pytest.mark.asyncio
async def test_concurrent_runs_each_act_with_their_own_installation(app_creds, probe):
    probe.delay = 0.01
    results = await asyncio.gather(
        agents.run_agent_loop(
            "a/app", run_id="ra", emitter=NullEmitter(), github_installation_id=11
        ),
        agents.run_agent_loop(
            "b/app", run_id="rb", emitter=NullEmitter(), github_installation_id=22
        ),
    )
    assert [r["status"] for r in results] == ["completed", "completed"]
    assert sorted(probe.seen) == [
        ("a/app", True, "inst-11-token"),
        ("b/app", True, "inst-22-token"),
    ]


@pytest.mark.asyncio
async def test_gh_proxy_resolves_to_the_runs_installation_client(
    monkeypatch, app_creds
):
    clients = {}

    def client_for(self):
        return clients.setdefault(self.installation_id, MagicMock())

    monkeypatch.setattr(github_app.GitHubCredentials, "client", client_for)
    token = agents._run_github_credentials.set(
        github_app.GitHubCredentials(installation_id=4)
    )
    try:
        agents.gh.get_repo("o/r")
    finally:
        agents._run_github_credentials.reset(token)
    clients[4].get_repo.assert_called_once_with("o/r")
    assert not agents.gh  # nothing bound outside a run


@pytest.mark.asyncio
async def test_run_without_installation_is_refused_in_app_mode(
    monkeypatch, app_creds, probe, caplog
):
    monkeypatch.setenv("GITHUB_TOKEN", "global-pat")
    with caplog.at_level(logging.ERROR, logger="github_app"):
        result = await agents.run_agent_loop(
            "a/app", run_id="r1", emitter=NullEmitter()
        )
    assert result["status"] == "failed"
    assert result["error_code"] == "GITHUB_AUTH_UNAVAILABLE"
    assert probe.seen == []  # the graph never ran
    assert "REFUSING" in caplog.text


@pytest.mark.asyncio
async def test_run_fails_fast_when_installation_token_cannot_be_minted(
    monkeypatch, probe
):
    monkeypatch.setattr(github_app, "GITHUB_APP_ID", "123")
    monkeypatch.setattr(github_app, "GITHUB_APP_PRIVATE_KEY", "pem")

    def revoked(_id, **_scope):
        raise RuntimeError("404 installation not found")

    monkeypatch.setattr(github_app, "get_installation_token_sync", revoked)
    result = await agents.run_agent_loop(
        "a/app", run_id="r1", emitter=NullEmitter(), github_installation_id=11
    )
    assert result["error_code"] == "GITHUB_AUTH_UNAVAILABLE"
    assert "404" not in result["error"]
    assert probe.seen == []


@pytest.mark.asyncio
async def test_pat_mode_run_uses_github_token(monkeypatch, probe):
    monkeypatch.setenv("GITHUB_TOKEN", "global-pat")
    await agents.run_agent_loop(
        "a/app", run_id="r1", emitter=NullEmitter(), github_installation_id=11
    )
    assert probe.seen == [("a/app", True, "global-pat")]


@pytest.mark.asyncio
async def test_pat_mode_without_token_runs_without_github_access(probe):
    result = await agents.run_agent_loop("a/app", run_id="r1", emitter=NullEmitter())
    assert result["status"] == "completed"
    assert probe.seen == [("a/app", False, None)]


def test_celery_task_passes_installation_id_to_the_real_run_agent_loop():
    kwargs = tasks._agent_loop_kwargs(
        repo_name="o/r", target_issue=None, run_id="r", github_installation_id=9
    )
    assert kwargs["github_installation_id"] == 9


# --- installation claim -----------------------------------------------------


def github_api(
    installations, login="alice", memberships=None, user_status=200, user_id=42
):
    """MockTransport for the user-token GitHub endpoints."""
    memberships = memberships or {}
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.path)
        assert request.headers["authorization"] == "Bearer gh-user-token"
        path = request.url.path
        if path == "/user":
            return httpx.Response(user_status, json={"login": login, "id": user_id})
        if path == "/user/installations":
            return httpx.Response(
                200,
                json={
                    "total_count": len(installations),
                    "installations": installations,
                },
            )
        if path.startswith("/user/memberships/orgs/"):
            org = path.rsplit("/", 1)[1]
            if org in memberships:
                return httpx.Response(200, json=memberships[org])
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    factory = lambda: httpx.AsyncClient(  # noqa: E731
        base_url=github_app.GITHUB_API_URL, transport=transport
    )
    return factory, seen


def installation(iid=555, login="acme", account_type="Organization"):
    return {
        "id": iid,
        "account": {"login": login, "type": account_type, "avatar_url": "https://a"},
        "target_type": account_type,
        "repository_selection": "selected",
        "permissions": {"contents": "write"},
        "events": ["push"],
        "suspended_at": None,
    }


@pytest.fixture
def claim(monkeypatch):
    def setup(role="owner", org="org-a", installations=(), **api):
        sb, store = fake_db(github_installations=installations)
        monkeypatch.setattr(agents, "supabase", sb)

        async def user():
            return {"user_id": "u", "org_id": org, "role": role, "email": "e"}

        app.dependency_overrides[main.get_current_user] = user
        return store

    def use_api(*args, **kwargs):
        factory, seen = github_api(*args, **kwargs)
        monkeypatch.setattr(github_app, "_user_api_client", factory)
        return seen

    return SimpleNamespace(setup=setup, api=use_api)


def post_claim(iid=555, token="gh-user-token"):
    headers = {"X-GitHub-User-Token": token} if token else {}
    return client.post(
        "/github/installations/claim", json={"installation_id": iid}, headers=headers
    )


def test_claim_links_installation_the_user_administers(claim):
    store = claim.setup()
    claim.api(
        [installation()], memberships={"acme": {"state": "active", "role": "admin"}}
    )

    r = post_claim()

    assert r.status_code == 200, r.text
    [row] = writes(store, "github_installations", "insert")
    assert row["id"] == 555 and row["org_id"] == "org-a"
    assert row["account_login"] == "acme" and row["permissions"] == {
        "contents": "write"
    }


def test_claim_of_users_own_account_installation(claim):
    store = claim.setup(role="admin")
    claim.api([installation(login="Alice", account_type="User")], login="alice")
    assert post_claim().status_code == 200
    assert writes(store, "github_installations", "insert")


def test_claim_rejects_installation_not_accessible_to_user(claim):
    store = claim.setup()
    claim.api([installation(iid=999)])
    r = post_claim(555)
    assert r.status_code == 403
    assert writes(store, "github_installations") == []


def test_claim_rejects_org_collaborator_who_is_not_org_admin(claim):
    store = claim.setup()
    claim.api(
        [installation()], memberships={"acme": {"state": "active", "role": "member"}}
    )
    assert post_claim().status_code == 403
    assert writes(store, "github_installations") == []


def test_claim_rejects_another_users_personal_installation(claim):
    store = claim.setup()
    claim.api([installation(login="mallory", account_type="User")], login="alice")
    assert post_claim().status_code == 403
    assert writes(store, "github_installations") == []


@pytest.mark.parametrize("role", ["member", "viewer", None])
def test_claim_requires_org_owner_or_admin(claim, role):
    store = claim.setup(role=role)
    seen = claim.api([installation()])
    assert post_claim().status_code == 403
    assert seen == []  # GitHub is not even asked
    assert writes(store, "github_installations") == []


def test_claim_requires_github_user_token(claim):
    claim.setup()
    seen = claim.api([installation()])
    assert post_claim(token=None).status_code == 400
    assert seen == []


def test_claim_with_invalid_user_token_is_401(claim):
    claim.setup()
    claim.api([installation()], user_status=401)
    assert post_claim().status_code == 401


def test_claim_never_moves_installation_from_another_org(claim):
    store = claim.setup(
        installations=[{"id": 555, "org_id": "org-b", "account_login": "acme"}]
    )
    claim.api(
        [installation()], memberships={"acme": {"state": "active", "role": "admin"}}
    )

    assert post_claim().status_code == 409
    assert writes(store, "github_installations") == []
    assert store["github_installations"][0]["org_id"] == "org-b"


def test_reclaim_by_same_org_updates_in_place(claim):
    store = claim.setup(
        installations=[{"id": 555, "org_id": "org-a", "account_login": "old"}]
    )
    claim.api(
        [installation()], memberships={"acme": {"state": "active", "role": "admin"}}
    )

    assert post_claim().status_code == 200
    [row] = store["github_installations"]
    assert row["org_id"] == "org-a" and row["account_login"] == "acme"
    assert writes(store, "github_installations", "insert") == []


# --- GitHub App install flow (install URL + setup callback) -----------------


@pytest.fixture
def install_flow(monkeypatch, claim):
    """Install flow configured; org-a's owner 'u' (GitHub user 42) can link.

    ``oauth`` records the OAuth code exchanges; ``oauth_result`` controls the
    token endpoint's answer.
    """
    monkeypatch.setenv("GITHUB_APP_SLUG", "auto-maintainer")
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "Iv1.client")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("DASHBOARD_URL", "https://app.example.com")
    exchanged = []
    answer = {"status": 200, "json": {"access_token": "gh-user-token"}}

    def handler(request: httpx.Request):
        from urllib.parse import parse_qs

        assert str(request.url) == github_app.GITHUB_OAUTH_TOKEN_URL
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        exchanged.append(form)
        return httpx.Response(answer["status"], json=answer["json"])

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        github_app, "_oauth_client", lambda: httpx.AsyncClient(transport=transport)
    )

    def setup(members=None, **kw):
        store = claim.setup(**kw)
        store["organization_members"] = (
            members
            if members is not None
            else [
                {
                    "user_id": "u",
                    "org_id": "org-a",
                    "role": "owner",
                    "joined_at": "2026-01-01T00:00:00Z",
                }
            ]
        )
        return store

    return SimpleNamespace(
        setup=setup, api=claim.api, exchanged=exchanged, answer=answer
    )


def state_for(user_id="u", org_id="org-a", github_user_id=42, now=None):
    return github_app.create_setup_state(user_id, org_id, github_user_id, now=now)


def setup_callback(query, **headers):
    return client.get(
        f"/github/app/setup{query}", headers=headers, follow_redirects=False
    )


def install_callback(iid=555, state=None, code="oauth-code", action="install"):
    from urllib.parse import urlencode

    params = {"installation_id": iid, "setup_action": action, "code": code}
    params["state"] = state_for() if state is None else state
    return setup_callback("?" + urlencode(params))


def redirect_params(response):
    from urllib.parse import parse_qs, urlsplit

    assert response.status_code == 303
    location = urlsplit(response.headers["location"])
    return location, {k: v[0] for k, v in parse_qs(location.query).items()}


def admin_installation():
    return dict(
        installations=[installation()],
        memberships={"acme": {"state": "active", "role": "admin"}},
    )


def test_install_url_carries_a_state_bound_to_the_caller(install_flow):
    from urllib.parse import parse_qs, urlsplit

    install_flow.setup()
    install_flow.api(**admin_installation())

    r = client.post(
        "/github/app/install-url", headers={"X-GitHub-User-Token": "gh-user-token"}
    )

    assert r.status_code == 200, r.text
    url = urlsplit(r.json()["install_url"])
    assert (url.netloc, url.path) == (
        "github.com",
        "/apps/auto-maintainer/installations/new",
    )
    [state] = parse_qs(url.query)["state"]
    payload = github_app.verify_setup_state(state)
    assert (payload["u"], payload["o"], payload["g"]) == ("u", "org-a", 42)
    assert r.json()["expires_in"] == github_app.SETUP_STATE_TTL_SECONDS


@pytest.mark.parametrize("role", ["member", "viewer", None])
def test_install_url_requires_org_owner_or_admin(install_flow, role):
    install_flow.setup(role=role)
    seen = install_flow.api([])
    r = client.post(
        "/github/app/install-url", headers={"X-GitHub-User-Token": "gh-user-token"}
    )
    assert r.status_code == 403
    assert seen == []


def test_install_url_requires_github_user_token(install_flow):
    install_flow.setup()
    assert client.post("/github/app/install-url").status_code == 400


def test_install_url_is_503_when_the_flow_is_not_configured(install_flow, monkeypatch):
    install_flow.setup()
    monkeypatch.delenv("GITHUB_APP_CLIENT_SECRET")
    r = client.post(
        "/github/app/install-url", headers={"X-GitHub-User-Token": "gh-user-token"}
    )
    assert r.status_code == 503


def test_setup_callback_links_a_verified_installation(install_flow):
    store = install_flow.setup()
    install_flow.api(**admin_installation())

    r = install_callback()

    location, params = redirect_params(r)
    assert (location.scheme, location.netloc, location.path) == (
        "https",
        "app.example.com",
        "/",
    )
    assert params == {"github_setup": "linked", "github_installation_id": "555"}
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["referrer-policy"] == "no-referrer"
    [row] = writes(store, "github_installations", "insert")
    assert row["id"] == 555 and row["org_id"] == "org-a"
    [form] = install_flow.exchanged
    assert form == {
        "client_id": "Iv1.client",
        "client_secret": "client-secret",
        "code": "oauth-code",
    }


def test_setup_callback_reclaim_by_same_org_updates_in_place(install_flow):
    store = install_flow.setup(
        installations=[{"id": 555, "org_id": "org-a", "account_login": "old"}]
    )
    install_flow.api(**admin_installation())
    _, params = redirect_params(install_callback())
    assert params["github_setup"] == "linked"
    [row] = store["github_installations"]
    assert row["account_login"] == "acme"


def test_setup_callback_never_moves_installation_from_another_org(install_flow):
    store = install_flow.setup(
        installations=[{"id": 555, "org_id": "org-b", "account_login": "acme"}]
    )
    install_flow.api(**admin_installation())
    _, params = redirect_params(install_callback())
    assert params == {"github_setup": "conflict"}
    assert writes(store, "github_installations") == []
    assert store["github_installations"][0]["org_id"] == "org-b"


def test_spoofed_installation_id_is_not_linked(install_flow):
    """A valid state and code, but an installation id the installer's token
    cannot access (someone else's installation): nothing is linked."""
    store = install_flow.setup()
    install_flow.api(
        [installation(iid=111)],
        memberships={"acme": {"state": "active", "role": "admin"}},
    )

    _, params = redirect_params(install_callback(iid=999))

    assert params == {"github_setup": "forbidden"}
    assert writes(store, "github_installations") == []


def test_installation_of_an_org_the_installer_does_not_own_is_not_linked(
    install_flow,
):
    store = install_flow.setup()
    install_flow.api(
        [installation()], memberships={"acme": {"state": "active", "role": "member"}}
    )
    _, params = redirect_params(install_callback())
    assert params == {"github_setup": "forbidden"}
    assert writes(store, "github_installations") == []


def _tampered(state):
    version, body, sig = state.split(".")
    payload = json.loads(github_app._unb64(body))
    payload["o"] = "org-evil"
    forged = github_app._b64(json.dumps(payload).encode())
    return f"{version}.{forged}.{sig}"


@pytest.mark.parametrize(
    "make_state",
    [
        lambda: "not-a-state",
        lambda: "v1.e30.AAAA",
        lambda: _tampered(state_for()),
        # Signed with a different client secret (another deployment).
        lambda: "v1." + state_for().split(".", 1)[1][:-4] + "AAAA",
        lambda: "v2." + state_for().split(".", 1)[1],
        lambda: "x" * 2000,
    ],
    ids=["garbage", "unsigned", "tampered-org", "bad-signature", "version", "huge"],
)
def test_forged_state_links_nothing(install_flow, make_state):
    store = install_flow.setup()
    seen = install_flow.api(**admin_installation())

    _, params = redirect_params(install_callback(state=make_state()))

    assert params == {"github_setup": "invalid"}
    assert install_flow.exchanged == []  # the code is not even redeemed
    assert seen == []
    assert writes(store, "github_installations") == []


def test_state_signed_with_another_secret_is_rejected(install_flow, monkeypatch):
    store = install_flow.setup()
    install_flow.api(**admin_installation())
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "other-secret")
    other = state_for()
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "client-secret")

    _, params = redirect_params(install_callback(state=other))

    assert params == {"github_setup": "invalid"}
    assert writes(store, "github_installations") == []


def test_expired_state_links_nothing(install_flow):
    import time as _time

    store = install_flow.setup()
    install_flow.api(**admin_installation())
    old = state_for(now=_time.time() - github_app.SETUP_STATE_TTL_SECONDS - 1)

    _, params = redirect_params(install_callback(state=old))

    assert params == {"github_setup": "expired"}
    assert install_flow.exchanged == []
    assert writes(store, "github_installations") == []


def test_state_from_another_github_user_links_nothing(install_flow):
    """Login-CSRF: Mallory mints a state for her org and gets Alice to
    install with it. Alice's OAuth token is not Mallory's GitHub user, so
    Alice's installation is not linked to Mallory's org."""
    store = install_flow.setup()
    install_flow.api(**admin_installation(), user_id=42)

    _, params = redirect_params(install_callback(state=state_for(github_user_id=7)))

    assert params == {"github_setup": "forbidden"}
    assert writes(store, "github_installations") == []


def test_state_for_a_user_who_is_not_an_org_admin_links_nothing(install_flow):
    """A state for a user who is not (or no longer) an owner/admin of the
    org it names: e.g. another user's state, or a demoted admin."""
    store = install_flow.setup(
        members=[
            {"user_id": "u", "org_id": "org-a", "role": "member", "joined_at": "x"},
            {"user_id": "v", "org_id": "org-a", "role": "admin", "joined_at": None},
        ]
    )
    install_flow.api(**admin_installation())

    for state in (state_for(), state_for(user_id="v"), state_for(org_id="org-b")):
        _, params = redirect_params(install_callback(state=state))
        assert params == {"github_setup": "forbidden"}
    assert install_flow.exchanged == []
    assert writes(store, "github_installations") == []


def test_rejected_oauth_code_links_nothing(install_flow):
    store = install_flow.setup()
    seen = install_flow.api(**admin_installation())
    install_flow.answer["json"] = {"error": "bad_verification_code"}

    _, params = redirect_params(install_callback())

    assert params == {"github_setup": "unauthorized"}
    assert seen == []
    assert writes(store, "github_installations") == []


@pytest.mark.parametrize(
    "query",
    [
        "?installation_id=555&setup_action=install",
        "?installation_id=555&setup_action=install&code=c",
        "?installation_id=555&setup_action=update&state=s",
    ],
)
def test_callback_without_state_and_code_is_not_trusted(install_flow, query):
    """An id alone (e.g. a crafted URL, or OAuth during install turned off)
    links nothing; it is not forwarded either."""
    store = install_flow.setup()
    seen = install_flow.api(**admin_installation())

    _, params = redirect_params(setup_callback(query))

    assert params == {"github_setup": "unverified"}
    assert seen == [] and install_flow.exchanged == []
    assert writes(store, "github_installations") == []


@pytest.mark.parametrize(
    "query",
    [
        "?installation_id=abc&setup_action=install",
        "?installation_id=-5&setup_action=install",
        "?installation_id=0&setup_action=install",
        "?installation_id=99999999999999999999&setup_action=install",
        "?installation_id=%EF%BC%95&setup_action=install",  # fullwidth digit
        "?installation_id=5%0d%0aSet-Cookie:x=1&setup_action=install",
        "?installation_id=5&setup_action=delete",
        "?installation_id=5&setup_action=install&state=s&code=a%0d%0ab",
        "?installation_id=5",
        "",
    ],
)
def test_setup_callback_rejects_malformed_input_without_echoing_it(install_flow, query):
    store = install_flow.setup()
    r = setup_callback(query)
    location, params = redirect_params(r)
    assert params == {"github_setup": "invalid"}
    assert location.path == "/"
    assert "set-cookie" not in r.headers
    assert writes(store, "github_installations") == []


def test_setup_callback_for_an_install_request_has_nothing_to_link(install_flow):
    install_flow.setup()
    _, params = redirect_params(setup_callback("?setup_action=request"))
    assert params == {"github_setup": "requested"}


@pytest.mark.parametrize(
    "extra",
    [
        "&redirect_uri=https://evil.example",
        "&next=//evil.example",
        "&return_to=https%3A%2F%2Fevil.example",
    ],
)
@pytest.mark.parametrize("valid", [True, False], ids=["linked", "rejected"])
def test_setup_callback_is_not_an_open_redirect(install_flow, extra, valid):
    from urllib.parse import urlencode

    install_flow.setup()
    install_flow.api(**admin_installation())
    state = state_for() if valid else "https://evil.example"
    query = urlencode(
        {
            "installation_id": 555,
            "setup_action": "install",
            "state": state,
            "code": "oauth-code",
        }
    )
    r = setup_callback(
        f"?{query}{extra}",
        host="evil.example",
        **{"x-forwarded-host": "evil.example", "referer": "https://evil.example/"},
    )
    location, params = redirect_params(r)
    assert location.netloc == "app.example.com"
    assert "evil" not in r.headers["location"]
    # Never the state or the OAuth code, nor any extra parameter.
    assert set(params) <= {"github_setup", "github_installation_id"}
    assert params["github_setup"] == ("linked" if valid else "invalid")


@pytest.mark.parametrize(
    "dashboard_url",
    ["", "javascript:alert(1)", "https://app.example.com/path", "//evil.example"],
)
def test_setup_callback_without_a_valid_dashboard_url_stays_on_this_origin(
    monkeypatch, install_flow, dashboard_url
):
    install_flow.setup()
    install_flow.api(**admin_installation())
    monkeypatch.setenv("DASHBOARD_URL", dashboard_url)
    r = install_callback()
    assert r.headers["location"].startswith("/?")
    assert not r.headers["location"].startswith("//")


# --- repository sync ---------------------------------------------------------


def _gh_repo(rid, full_name):
    owner, name = full_name.split("/")
    return {
        "id": rid,
        "full_name": full_name,
        "name": name,
        "owner_login": owner,
        "owner_type": "Organization",
        "private": True,
        "default_branch": "main",
        "description": None,
        "language": None,
        "topics": [],
        "avatar_url": "",
        "html_url": "",
        "archived": False,
        "disabled": False,
        "pushed_at": None,
    }


@pytest.mark.asyncio
async def test_sync_never_reassigns_a_repository_owned_by_another_org(caplog):
    sb, store = fake_db(
        repositories=[
            {"id": 1, "org_id": "org-victim", "full_name": "v/app"},
            {"id": 2, "org_id": "org-a", "full_name": "a/mine"},
        ]
    )
    repos = [_gh_repo(1, "v/app"), _gh_repo(2, "a/mine"), _gh_repo(3, "a/new")]

    with patch.object(
        github_app, "get_installation_repositories", new=AsyncMock(return_value=repos)
    ), caplog.at_level(logging.WARNING, logger="github_app"):
        synced = await github_app.sync_installation_repositories(sb, 7, "org-a")

    assert synced == 2
    by_id = {r["id"]: r for r in store["repositories"]}
    assert by_id[1]["org_id"] == "org-victim"
    assert "github_installation_id" not in by_id[1]
    assert by_id[2]["org_id"] == "org-a" and by_id[2]["github_installation_id"] == 7
    assert by_id[3]["org_id"] == "org-a"
    assert [w["id"] for w in writes(store, "repositories", "upsert")] == [2, 3]
    assert "already belongs to org org-victim" in caplog.text


class ImmutableOrgRows(Rows):
    """Rows plus the repositories_org_immutable trigger: an upsert that
    would change an existing repository's org_id fails like PostgREST."""

    def execute(self):
        kind, row = self.op
        if self.table == "repositories" and kind == "upsert":
            for existing in self.store.get("repositories", []):
                if existing["id"] == row["id"] and existing["org_id"] != row["org_id"]:
                    raise APIError(
                        {
                            "message": "repository_org_immutable: repository "
                            f"{row['id']} belongs to another organization",
                            "code": "23514",
                        }
                    )
        return super().execute()


@pytest.mark.asyncio
async def test_sync_race_is_stopped_by_the_database_guard(monkeypatch, caplog):
    """Another org's row appears after this sync's ownership read but before
    its upsert: the database rejects the reassignment and sync skips it."""
    store = {"repositories": [{"id": 1, "org_id": "org-victim", "full_name": "v/x"}]}
    sb = MagicMock()
    sb.table.side_effect = lambda name: ImmutableOrgRows(store, name)
    # The ownership read ran before the victim's row was inserted.
    monkeypatch.setattr(
        github_app, "_existing_repository_orgs", AsyncMock(return_value={})
    )
    repos = [_gh_repo(1, "v/x"), _gh_repo(2, "a/mine")]

    with patch.object(
        github_app, "get_installation_repositories", new=AsyncMock(return_value=repos)
    ), caplog.at_level(logging.WARNING, logger="github_app"):
        synced = await github_app.sync_installation_repositories(sb, 7, "org-a")

    assert synced == 1
    by_id = {r["id"]: r for r in store["repositories"]}
    assert by_id[1]["org_id"] == "org-victim"
    assert by_id[2]["org_id"] == "org-a"
    assert "another org linked it concurrently" in caplog.text
    # A conflict is expected under concurrency, not an error.
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_schema_guard_against_repository_org_change_is_re_runnable():
    sql = (Path(__file__).resolve().parent.parent / "supabase_schema.sql").read_text(
        encoding="utf-8"
    )
    assert "CREATE OR REPLACE FUNCTION prevent_repository_org_change()" in sql
    drop = sql.index(
        "DROP TRIGGER IF EXISTS repositories_org_immutable ON repositories"
    )
    create = sql.index(
        "CREATE TRIGGER repositories_org_immutable BEFORE UPDATE OF org_id"
    )
    assert drop < create
    assert "NEW.org_id IS DISTINCT FROM OLD.org_id" in sql
    assert github_app.REPOSITORY_ORG_CONFLICT_MARKER + ":" in sql
