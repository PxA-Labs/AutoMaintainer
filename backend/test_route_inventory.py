"""Route inventory: authentication, tenant isolation and admin checks (#210).

Built from ``app.routes`` (HTTP and WebSocket), not from a hand-kept list of
URLs, and probed over HTTP against a two-organization fake database:

- every route is classified below; a route added without a classification
  fails test_every_route_is_classified;
- every non-public HTTP route answers 401 without a token and with an invalid
  one, so a new route that forgets its auth dependency fails
  test_non_public_http_route_rejects_missing_and_invalid_token even before it
  is classified;
- every tenant route answers another organization's repository, run or
  repository id exactly as it answers a missing one (404), with no side
  effects, and still serves the owning organization;
- routes that change org-wide settings or state refuse plain members (403);
- WebSockets refuse a missing token with 4401 and another org's repository
  exactly like a missing one, with 4403.

test_route_auth.py complements this with a static check of the dependency
tree; this file checks behaviour.
"""

import re
import sys
import types
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

import anyio
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocketDisconnect

import agents
import main
from main import app

client = TestClient(app)

ORG_A, ORG_B = "org-a", "org-b"
TOKENS = {
    "tok-a-owner": ("user-a", ORG_A, "owner"),
    "tok-a-member": ("user-a-member", ORG_A, "member"),
    "tok-b-owner": ("user-b", ORG_B, "owner"),
}
RUN_A, RUN_B, RUN_MISSING = (str(uuid.uuid4()) for _ in range(3))
REPO_A = {"id": 1, "org_id": ORG_A, "full_name": "a/app"}
REPO_B = {"id": 2, "org_id": ORG_B, "full_name": "b/secret"}

# --- Classification ------------------------------------------------------------

# The only routes reachable without a Supabase token, each with its reason.
PUBLIC = {
    ("GET", "/healthz"): "Liveness probe for Render/Docker; returns a constant.",
    ("GET", "/healthz/supabase"): "Readiness probe; errors are generic, no host.",
    ("GET", "/healthz/observability"): "Readiness probe; booleans only.",
    ("POST", "/webhooks/github"): (
        "GitHub cannot present a Supabase token. Authenticated by HMAC instead: "
        "503 without GITHUB_WEBHOOK_SECRET, X-Hub-Signature-256 checked over "
        "the raw body with compare_digest."
    ),
    ("GET", "/github/app/setup"): (
        "GitHub App setup/OAuth callback (a browser redirect from GitHub). "
        "Requires the HMAC-signed, expiring state issued by the authenticated "
        "POST /github/app/install-url plus the installer's OAuth code, and "
        "verifies ownership with GitHub before linking anything."
    ),
    ("GET", "/openapi.json"): "Generated API schema; no data (repo is public).",
    ("GET", "/docs"): "Generated API docs; no data.",
    ("GET", "/docs/oauth2-redirect"): "Generated API docs helper; no data.",
    ("GET", "/redoc"): "Generated API docs; no data.",
}

# Routes that name a tenant resource. Each builder turns a target (repo name,
# repository id, run id) into (method, url, kwargs).
TENANT = {
    ("GET", "/repo/{repo_name:path}/tree"): lambda t: (
        "get",
        f"/repo/{t.repo}/tree",
        {},
    ),
    ("GET", "/repo/{repo_name:path}/search"): lambda t: (
        "get",
        f"/repo/{t.repo}/search?q=x",
        {},
    ),
    ("GET", "/repo/{repo_name:path}/file"): lambda t: (
        "get",
        f"/repo/{t.repo}/file?file_path=a.py",
        {},
    ),
    ("POST", "/repo/{repo_name:path}/file"): lambda t: (
        "post",
        f"/repo/{t.repo}/file",
        {"json": {"file_path": "a.py", "content": "y"}},
    ),
    ("POST", "/repo/{repo_name:path}/file/create"): lambda t: (
        "post",
        f"/repo/{t.repo}/file/create",
        {"json": {"file_path": "b.py"}},
    ),
    ("DELETE", "/repo/{repo_name:path}/file"): lambda t: (
        "delete",
        f"/repo/{t.repo}/file?file_path=a.py",
        {},
    ),
    ("POST", "/repo/{repo_name:path}/propose-changes"): lambda t: (
        "post",
        f"/repo/{t.repo}/propose-changes",
        {"json": {"title": "t", "changes": [{"path": "a.py", "content": "y"}]}},
    ),
    ("POST", "/start"): lambda t: ("post", "/start", {"json": {"repo_name": t.repo}}),
    ("POST", "/stop"): lambda t: ("post", "/stop", {"json": {"run_id": t.run}}),
    ("GET", "/status/{run_id}"): lambda t: ("get", f"/status/{t.run}", {}),
    ("GET", "/runs/{run_id}/verifications"): lambda t: (
        "get",
        f"/runs/{t.run}/verifications",
        {},
    ),
    ("GET", "/runs/{run_id}/decisions"): lambda t: (
        "get",
        f"/runs/{t.run}/decisions",
        {},
    ),
    ("GET", "/repositories/{repository_id}/merge-policy"): lambda t: (
        "get",
        f"/repositories/{t.repository_id}/merge-policy",
        {},
    ),
    ("PUT", "/repositories/{repository_id}/merge-policy"): lambda t: (
        "put",
        f"/repositories/{t.repository_id}/merge-policy",
        {"json": {"merge_policy": "human"}},
    ),
}

# Authenticated routes that accept no foreign identifier: they act on the
# caller's own organization only (asserted for the list routes below).
ORG_SCOPED = {
    ("GET", "/runs"): "Lists runs filtered by the caller's org_id.",
    ("GET", "/admin/metrics"): "Admin; every query filtered by org_id.",
    ("GET", "/admin/runs"): "Admin; filtered by org_id.",
    ("POST", "/assist/inline"): (
        "Reads only the code in the request body (repo_name is unused); "
        "rate-limited per user and per org."
    ),
    ("POST", "/github/app/install-url"): "Admin; state bound to caller and org.",
    ("POST", "/github/installations/claim"): (
        "Admin; the installation must be accessible to the caller's GitHub "
        "token and owned by its account (verified with GitHub). One linked to "
        "another org answers 409 and is never moved (test_tenant_tokens.py); "
        "that is not an enumeration oracle, since the caller has already "
        "proven GitHub access to it."
    ),
}

WEBSOCKETS = {
    ("WS", "/api/terminal/ws"): (
        "Interactive shell. Off unless ENABLE_TERMINAL=true; exact Origin; "
        "token as subprotocol; TERMINAL_ADMIN_USER_IDS; repo_url tenant check."
    ),
}

# Routes that change org-wide settings or state: owners and admins only.
# (method, url, kwargs) sent as a plain member of org A.
ADMIN_ONLY = [
    (
        "put",
        f"/repositories/{REPO_A['id']}/merge-policy",
        {"json": {"merge_policy": "human"}},
    ),
    ("post", "/stop", {}),  # bulk stop: every run in the org
    ("post", "/github/installations/claim", {"json": {"installation_id": 1}}),
    ("post", "/github/app/install-url", {}),
    ("get", "/admin/metrics", {}),
    ("get", "/admin/runs", {}),
]

# --- Fakes ---------------------------------------------------------------------


class FakeQuery:
    """PostgREST builder over in-memory rows: eq, in_, not_.is_, update,
    insert; other modifiers are accepted and ignored."""

    def __init__(self, db, table):
        self.rows = db.setdefault(table, [])
        self.filters = []
        self.single = False
        self.negate = False
        self.op, self.payload = "select", None

    def _same(self, *a, **k):
        return self

    select = order = limit = range = gte = _same

    def maybe_single(self):
        self.single = True
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: str(r.get(col)) == str(val))
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    @property
    def not_(self):
        self.negate = True
        return self

    def is_(self, col, val):
        negate, self.negate = self.negate, False
        self.filters.append(lambda r: (r.get(col) is None) != negate)
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def execute(self):
        if self.op == "insert":
            self.rows.append(dict(self.payload))
            return SimpleNamespace(data=[dict(self.payload)])
        rows = [r for r in self.rows if all(f(r) for f in self.filters)]
        if self.op == "update":
            for r in rows:
                r.update(self.payload)
            return SimpleNamespace(data=[dict(r) for r in rows])
        if self.single:
            return SimpleNamespace(data=dict(rows[0]) if rows else None)
        return SimpleNamespace(data=[dict(r) for r in rows])


def _get_user(token):
    if token not in TOKENS:
        raise Exception("invalid JWT")
    user_id = TOKENS[token][0]
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id, email=f"{user_id}@example.com")
    )


@pytest.fixture
def db(monkeypatch, tmp_path):
    """Two organizations, each with one repository (cloned) and one running run."""
    monkeypatch.delenv("AUTH_DEV_BYPASS", raising=False)
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "fake")
    tables = {
        "organization_members": [
            {"user_id": u, "org_id": o, "role": r, "joined_at": "t"}
            for u, o, r in TOKENS.values()
        ],
        "repositories": [
            {**repo, "github_installation_id": 10 + repo["id"], "merge_policy": None}
            for repo in (REPO_A, REPO_B)
        ],
        "runs": [
            {"id": RUN_A, "org_id": ORG_A, "status": "running"},
            {"id": RUN_B, "org_id": ORG_B, "status": "running"},
        ],
    }
    sb = SimpleNamespace(
        table=lambda name: FakeQuery(tables, name),
        rpc=lambda *a, **k: FakeQuery(tables, "__rpc"),
        auth=SimpleNamespace(get_user=_get_user),
    )
    monkeypatch.setattr(agents, "supabase", sb, raising=False)

    from workspace import get_safe_repo_dir

    for repo in (REPO_A, REPO_B):
        clone = get_safe_repo_dir(repo["full_name"], repo["id"])
        clone.mkdir(parents=True)
        (clone / "a.py").write_text("x", encoding="utf-8")
    return tables


@pytest.fixture
def side_effects(monkeypatch):
    """Record every GitHub client, cancellation and queued run."""
    seen = {"github": [], "cancel": [], "queued": []}
    gh_repo = MagicMock()
    gh_repo.default_branch = "main"
    gh_repo.get_contents.return_value = SimpleNamespace(path="a.py", sha="s")
    gh_repo.get_branch.return_value = SimpleNamespace(protected=False)
    gh_repo.get_git_ref.return_value = SimpleNamespace(object=SimpleNamespace(sha="b"))
    gh_repo.create_pull.return_value = SimpleNamespace(number=1, html_url="u")

    def github(*a, **k):
        seen["github"].append(a)
        return MagicMock(get_repo=lambda _name: gh_repo)

    def cancel(run_id):
        seen["cancel"].append(run_id)
        return {"success": True}

    async def no_worker():
        return False

    monkeypatch.setattr(main, "Github", github)
    monkeypatch.setattr(main, "cancel_run_task", cancel)
    monkeypatch.setattr(main, "agent_worker_available", no_worker)
    monkeypatch.setattr(
        main,
        "run_agent_loop_task",
        SimpleNamespace(apply_async=lambda *a, **k: seen["queued"].append(a)),
    )
    return seen


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def send(token, request):
    method, url, kwargs = request
    headers = bearer(token) if token else {}
    return getattr(client, method)(url, headers=headers, **kwargs)


FOREIGN = SimpleNamespace(
    repo=REPO_B["full_name"], repository_id=REPO_B["id"], run=RUN_B
)
MISSING = SimpleNamespace(repo="nobody/missing", repository_id=999, run=RUN_MISSING)

# --- Inventory -----------------------------------------------------------------


def discovered_routes() -> set:
    found = set()
    for route in app.routes:
        if isinstance(route, Mount) and isinstance(route.app, StaticFiles):
            # The static dashboard export: compiled UI and public
            # NEXT_PUBLIC_* settings only (see ARCHITECTURE.md). Any other
            # Mount (a sub-application with its own routes) falls through
            # to UNKNOWN below and must be classified.
            continue
        if isinstance(route, (APIWebSocketRoute, WebSocketRoute)):
            found.add(("WS", route.path))
        elif isinstance(route, (APIRoute, Route)):
            for method in route.methods - {"HEAD"}:
                found.add((method, route.path))
        else:  # pragma: no cover - a new kind of route needs a decision here
            found.add(("UNKNOWN", repr(route)))
    return found


CLASSIFIED = [PUBLIC, TENANT, ORG_SCOPED, WEBSOCKETS]


def test_every_route_is_classified():
    classified = set().union(*CLASSIFIED)
    discovered = discovered_routes()

    unclassified = discovered - classified
    assert not unclassified, (
        f"Routes without an auth classification: {sorted(unclassified)}. Add each "
        "to TENANT (with a request builder), ORG_SCOPED or WEBSOCKETS, or to "
        "PUBLIC with the reason it needs no token."
    )
    stale = classified - discovered
    assert not stale, f"Classified routes that no longer exist: {sorted(stale)}"


def test_classifications_do_not_overlap():
    total = sum(len(group) for group in CLASSIFIED)
    assert total == len(set().union(*CLASSIFIED))


# Parameter names that identify a tenant-owned object. A route taking one
# belongs in TENANT, where the cross-org probe covers it, not in ORG_SCOPED.
TENANT_IDENTIFIERS = re.compile(r"repo|run_id|repository|installation|org_id")
# ORG_SCOPED routes whose tenant-looking parameter is handled otherwise; the
# reason is in ORG_SCOPED.
ORG_SCOPED_IDENTIFIER_EXEMPT = {
    ("POST", "/assist/inline"): {"repo_name"},
    ("POST", "/github/installations/claim"): {"installation_id"},
}


def _parameter_names(route: APIRoute) -> set:
    dependant = route.dependant
    names = {p.alias for p in dependant.path_params + dependant.query_params}
    for body in dependant.body_params:
        fields = getattr(body.field_info.annotation, "model_fields", None)
        names |= set(fields) if fields else {body.alias}
    return names


def test_org_scoped_routes_take_no_tenant_identifier():
    """An ORG_SCOPED route never takes a repository, run or installation id.

    ORG_SCOPED only needs a one-line reason, so without this check a route
    that takes another org's identifier could be filed there and skip the
    cross-org probe.
    """
    offenders = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"HEAD"}:
            key = (method, route.path)
            if key not in ORG_SCOPED:
                continue
            tenant_params = {
                name
                for name in _parameter_names(route)
                if TENANT_IDENTIFIERS.search(name)
            } - ORG_SCOPED_IDENTIFIER_EXEMPT.get(key, set())
            if tenant_params:
                offenders[key] = sorted(tenant_params)
    assert not offenders, (
        f"ORG_SCOPED routes taking tenant identifiers: {offenders}. Move them to "
        "TENANT with a request builder."
    )


def test_org_scoped_identifier_check_detects_a_misfiled_route():
    from fastapi import FastAPI

    probe = FastAPI()

    @probe.get("/things/{run_id}")
    def _thing(run_id: str, repo_name: str = ""):  # pragma: no cover - not called
        return {}

    (route,) = [r for r in probe.routes if isinstance(r, APIRoute)]
    assert {"run_id", "repo_name"} <= _parameter_names(route)


def _sample_url(path: str) -> str:
    samples = {"repo_name": "a/app", "run_id": RUN_A, "repository_id": "1"}
    return re.sub(r"\{(\w+)(?::\w+)?\}", lambda m: samples.get(m.group(1), "x"), path)


NON_PUBLIC_HTTP = sorted(
    key for key in discovered_routes() if key[0] != "WS" and key not in PUBLIC
)


@pytest.mark.parametrize(
    "method,path", NON_PUBLIC_HTTP, ids=[f"{m} {p}" for m, p in NON_PUBLIC_HTTP]
)
@pytest.mark.parametrize("token", [None, "not-a-real-token"])
def test_non_public_http_route_rejects_missing_and_invalid_token(
    db, side_effects, method, path, token
):
    response = client.request(
        method, _sample_url(path), headers=bearer(token) if token else {}
    )

    assert (
        response.status_code == 401
    ), f"{method} {path} answered {response.status_code} without a valid token"
    assert side_effects == {"github": [], "cancel": [], "queued": []}


TENANT_KEYS = sorted(TENANT)


@pytest.mark.parametrize("key", TENANT_KEYS, ids=[f"{m} {p}" for m, p in TENANT_KEYS])
def test_cross_org_resource_is_indistinguishable_from_missing(db, side_effects, key):
    """An org owner (no role check can stop them) probing org B's resource."""
    build = TENANT[key]

    foreign = send("tok-a-owner", build(FOREIGN))
    missing = send("tok-a-owner", build(MISSING))

    assert foreign.status_code == missing.status_code == 404, (
        foreign.status_code,
        missing.status_code,
    )
    assert side_effects == {"github": [], "cancel": [], "queued": []}
    # Same body too, once the identifier the caller sent is masked.
    assert _masked(foreign.text, FOREIGN) == _masked(missing.text, MISSING)
    assert db["repositories"][1]["merge_policy"] is None


def _masked(body: str, target) -> str:
    for value in (target.repo, str(target.repository_id), target.run):
        body = body.replace(value, "<id>")
    return body


@pytest.mark.parametrize("key", TENANT_KEYS, ids=[f"{m} {p}" for m, p in TENANT_KEYS])
def test_owning_org_passes_the_tenant_check(db, side_effects, key):
    """Control for the test above: the same request from org B is served, so
    its 404 comes from the tenant check and not from a broken fixture."""
    response = send("tok-b-owner", TENANT[key](FOREIGN))

    # /start stops at the worker check (no Celery in tests): 503, after the
    # repository was authorized.
    expected = 503 if key == ("POST", "/start") else 200
    assert response.status_code == expected, response.text


@pytest.mark.parametrize(
    "request_", ADMIN_ONLY, ids=[f"{m.upper()} {u}" for m, u, _ in ADMIN_ONLY]
)
def test_org_wide_changes_require_an_admin(db, side_effects, request_):
    response = send("tok-a-member", request_)

    assert response.status_code == 403, response.text
    assert side_effects == {"github": [], "cancel": [], "queued": []}
    assert db["repositories"][0]["merge_policy"] is None


def test_bulk_stop_cancels_only_the_callers_org(db, side_effects):
    response = send("tok-a-owner", ("post", "/stop", {}))

    assert response.status_code == 200, response.text
    assert side_effects["cancel"] == [RUN_A]


@pytest.mark.parametrize("path", ["/runs", "/admin/runs"])
def test_run_lists_contain_only_the_callers_org(db, side_effects, path):
    response = send("tok-a-owner", ("get", path, {}))

    assert response.status_code == 200, response.text
    assert [run["id"] for run in response.json()["runs"]] == [RUN_A]


# --- WebSockets ----------------------------------------------------------------

WS_PATHS = sorted(path for kind, path in discovered_routes() if kind == "WS")


@pytest.fixture
def terminal(monkeypatch):
    """Terminal enabled for the org A owner, with a PTY that records spawns."""
    monkeypatch.setenv("ENABLE_TERMINAL", "true")
    monkeypatch.setenv("TERMINAL_ADMIN_USER_IDS", "user-a")
    spawned = []

    class FakePTY:
        pid = None

        def __init__(self, *_a):
            pass

        def spawn(self, *a, **k):
            spawned.append(k.get("cwd"))
            return True

        def read(self, *_a):
            return "$ "

        def write(self, data):
            return len(data)

    monkeypatch.setitem(sys.modules, "winpty", types.SimpleNamespace(PTY=FakePTY))
    monkeypatch.setattr(main, "_use_winpty", lambda: True)
    return spawned


WS_PROBE_TIMEOUT_S = 5.0


class _AcceptedAndWaited(Exception):
    pass


def _receive_or_fail(ws) -> str:
    """ws.receive_text() with a deadline.

    A route that accepts without authenticating and then waits for client
    input would otherwise block the probe forever instead of failing it. Uses
    the session's private stream because Starlette's TestClient has no
    receive timeout.
    """

    async def receive():
        with anyio.fail_after(WS_PROBE_TIMEOUT_S):
            return await ws._send_rx.receive()

    try:
        message = ws.portal.call(receive)
    except TimeoutError:
        raise _AcceptedAndWaited from None
    ws._raise_on_close(message)
    return message["text"]


def ws_close_code(url, subprotocols=None) -> int:
    # Closing the session makes such a route raise its own
    # WebSocketDisconnect(1000) on teardown, which would replace an exception
    # raised inside the block; record the timeout and fail after the block.
    waited = False
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            url,
            headers={"origin": "http://localhost:3000"},
            subprotocols=subprotocols,
        ) as ws:
            try:
                _receive_or_fail(ws)
            except _AcceptedAndWaited:
                waited = True
    if waited:
        pytest.fail(
            f"{url} accepted the handshake and waited for input instead of "
            "closing: it does not authenticate before accept()"
        )
    return excinfo.value.code


@pytest.mark.parametrize("path", WS_PATHS)
@pytest.mark.parametrize(
    "subprotocols", [None, ["bearer"], ["bearer", "not-a-real-token"]]
)
def test_websocket_rejects_missing_and_invalid_token(db, terminal, path, subprotocols):
    assert ws_close_code(path, subprotocols) == main.WS_CLOSE_UNAUTHENTICATED
    assert terminal == []


def test_terminal_cross_org_repo_is_indistinguishable_from_missing(db, terminal):
    auth = ["bearer", "tok-a-owner"]
    foreign = ws_close_code(f"/api/terminal/ws?repo_url={REPO_B['full_name']}", auth)
    missing = ws_close_code("/api/terminal/ws?repo_url=nobody/missing", auth)

    assert foreign == missing == main.WS_CLOSE_FORBIDDEN
    assert terminal == []


def test_terminal_own_repo_is_served(db, terminal):
    """Control: the same caller gets a shell in its own org's clone."""
    with client.websocket_connect(
        f"/api/terminal/ws?repo_url={REPO_A['full_name']}",
        headers={"origin": "http://localhost:3000"},
        subprotocols=["bearer", "tok-a-owner"],
    ) as ws:
        assert ws.receive_text() == "$ "

    assert len(terminal) == 1 and terminal[0].endswith("1")


def test_refusal_echoes_only_the_bearer_marker(db, terminal):
    """The close code reaches browsers only on a completed handshake; the
    token itself is never sent back."""
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(
            "/api/terminal/ws",
            headers={"origin": "http://localhost:3000"},
            subprotocols=["bearer", "not-a-real-token"],
        ) as ws:
            assert ws.accepted_subprotocol == "bearer"
            ws.receive_text()


@pytest.mark.parametrize("enabled", ["true", ""])
@pytest.mark.parametrize("subprotocols", [None, ["bearer", "not-a-real-token"]])
def test_terminal_state_is_not_revealed_without_a_token(
    db, terminal, monkeypatch, enabled, subprotocols
):
    """Unauthenticated callers get 4401 whether the terminal is on or off."""
    monkeypatch.setenv("ENABLE_TERMINAL", enabled)

    code = ws_close_code("/api/terminal/ws", subprotocols)

    assert code == main.WS_CLOSE_UNAUTHENTICATED
    assert terminal == []


def test_terminal_is_off_by_default(db, terminal, monkeypatch):
    monkeypatch.delenv("ENABLE_TERMINAL")

    code = ws_close_code("/api/terminal/ws", ["bearer", "tok-a-owner"])

    assert code == main.WS_CLOSE_FORBIDDEN
    assert terminal == []
