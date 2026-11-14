import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main
from main import app

client = TestClient(app)


def allowed_origin():
    """Fresh headers dict per call.

    TestClient.websocket_connect mutates the mapping it is given (it
    setdefault()s sec-websocket-protocol into it), so a shared module-level
    constant leaks the first call's subprotocol into every later connection.
    """
    return {"origin": "http://localhost:3000"}


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


@pytest.fixture(autouse=True)
def terminal_enabled(monkeypatch):
    """Opt in, and allowlist the test identities, by default.

    Without this, every rejection test below would pass trivially on the
    disabled-by-default gate instead of the check it claims to cover.
    """
    monkeypatch.setenv("ENABLE_TERMINAL", "true")
    monkeypatch.setenv("TERMINAL_ADMIN_USER_IDS", "test-user, user-1")
    yield


@pytest.fixture(autouse=True)
def fake_pty(monkeypatch):
    """Route every terminal through a fake pywinpty, on any OS.

    Tests never start a real shell, and the spawn arguments (cwd, env) can be
    asserted. Also pins the module name: the package is pywinpty, but the
    module it installs is `winpty` (the old `import pywinpty` always failed).
    """
    import sys
    import time
    import types

    captured = {"spawned": False, "cwd": None, "env": None, "size": None}

    class FakePTY:
        pid = None

        def __init__(self, cols, rows):
            captured["size"] = (cols, rows)

        def spawn(self, appname, cmdline=None, cwd=None, env=None):
            captured.update(spawned=True, cwd=cwd, env=env)
            return True

        def read(self, *a, **k):
            time.sleep(0.01)
            return ""

        def write(self, data):
            return len(data)

        def set_size(self, cols, rows):
            captured["size"] = (cols, rows)

    monkeypatch.setitem(sys.modules, "winpty", types.SimpleNamespace(PTY=FakePTY))
    monkeypatch.setattr(main, "_use_winpty", lambda: True)
    yield captured


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
        with client.websocket_connect("/api/terminal/ws", headers=allowed_origin()):
            pass

    assert excinfo.value.code == 1008


def test_terminal_websocket_rejects_malformed_subprotocol():
    """Only a ["bearer", <token>] pair is accepted."""
    for protocols in (["bearer"], ["notbearer", "tok"], ["tok", "bearer"]):
        with pytest.raises(WebSocketDisconnect) as excinfo:
            with client.websocket_connect(
                "/api/terminal/ws",
                headers=allowed_origin(),
                subprotocols=protocols,
            ):
                pass

        assert excinfo.value.code == 1008, f"{protocols} was not rejected"


@pytest.mark.parametrize("environment", [None, "development", "production"])
def test_terminal_websocket_disabled_unless_opted_in(
    monkeypatch, authenticated, environment
):
    """The shell is opt-in everywhere, not just when ENVIRONMENT=production.

    Regression guard: the shipped Dockerfile never sets ENVIRONMENT, so a
    production-only gate left the terminal enabled on real deployments.
    """
    monkeypatch.delenv("ENABLE_TERMINAL", raising=False)
    if environment is None:
        monkeypatch.delenv("ENVIRONMENT", raising=False)
    else:
        monkeypatch.setenv("ENVIRONMENT", environment)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/terminal/ws",
            headers=allowed_origin(),
            subprotocols=["bearer", "tok"],
        ):
            pass

    assert excinfo.value.code == 1008


@pytest.mark.parametrize("allowlist", ["", "someone-else", " , ,"])
def test_terminal_websocket_requires_admin_allowlist(
    monkeypatch, authenticated, allowlist
):
    """Org membership is not enough: orgs are self-serve, so every signup owns
    one. Only user ids on TERMINAL_ADMIN_USER_IDS get a shell; empty = nobody.
    """
    monkeypatch.setenv("TERMINAL_ADMIN_USER_IDS", allowlist)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/terminal/ws",
            headers=allowed_origin(),
            subprotocols=["bearer", "tok"],
        ):
            pass

    assert excinfo.value.code == 1008


def test_terminal_dev_bypass_alone_does_not_grant_shell(monkeypatch):
    """AUTH_DEV_BYPASS with the terminal enabled must still hit the allowlist."""
    monkeypatch.setenv("AUTH_DEV_BYPASS", "true")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("AUTH_DEV_USER_ID", raising=False)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect(
            "/api/terminal/ws",
            headers=allowed_origin(),
            subprotocols=["bearer", "tok"],
        ):
            pass

    assert excinfo.value.code == 1008


# Every secret-bearing variable the backend reads, plus generic patterns.
SERVER_SECRETS = (
    "SUPABASE_URL",
    "SUPABASE_SERVICE_KEY",
    "SUPABASE_ANON_KEY",
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_WEBHOOK_SECRET",
    "GROQ_API_KEY",
    "GROQ_API_KEY_1",
    "GROQ_API_KEY_2",
    "GROQ_API_KEY_7",
    "DATABASE_URL",
    "REDIS_URL",
    "SOME_SERVICE_TOKEN",
    "ANOTHER_SECRET",
    "OTEL_EXPORTER_OTLP_HEADERS",
)


@pytest.mark.parametrize("windows", [False, True])
def test_terminal_child_env_withholds_server_secrets(monkeypatch, windows):
    """The spawned shell gets an allowlisted env, never the server's secrets."""
    for name in SERVER_SECRETS:
        monkeypatch.setenv(name, "s3cret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")

    env = main._terminal_child_env(windows=windows)

    allowed = set(main._TERMINAL_ENV_ALLOWLIST) | {"TERM"}
    if windows:
        allowed |= set(main._TERMINAL_ENV_ALLOWLIST_WIN32)
    unexpected = {
        k for k in env if k.upper() not in allowed and not k.startswith("LC_")
    }
    assert not unexpected, f"non-allowlisted variables reached the shell: {unexpected}"
    assert "s3cret" not in env.values()
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LC_ALL"] == "C.UTF-8"
    assert env["TERM"] == "xterm-256color"


@pytest.mark.parametrize(
    "repo_url,expected",
    [
        ("owner/repo", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("github.com/owner/repo/", None),
        ("https://evil.com/owner/repo", None),
        ("owner/repo\n", None),
        ("owner/../repo", None),
        ("../../etc", None),
        ("../repo", None),
    ],
)
def test_parse_terminal_repo(repo_url, expected):
    """The dashboard sends "owner/repo"; a github.com URL is accepted too."""
    assert main._parse_terminal_repo(repo_url) == expected


def _connect_and_resize(path="/api/terminal/ws"):
    with client.websocket_connect(
        path,
        headers=allowed_origin(),
        subprotocols=["bearer", "valid-token"],
    ) as ws:
        ws.send_text('{"type":"resize", "cols":100, "rows":30}')


def test_terminal_websocket_allows_authenticated_caller(authenticated, fake_pty):
    """A trusted origin, a valid token and an allowlisted user get a shell."""
    _connect_and_resize()

    assert fake_pty["spawned"], "allowlisted caller did not get a shell"
    assert fake_pty["cwd"] is None


def test_terminal_spawns_winpty_with_scrubbed_env(monkeypatch, authenticated, fake_pty):
    """The Windows path used to inherit the full server environment."""
    monkeypatch.setenv("SUPABASE_SERVICE_KEY", "s3cret")
    monkeypatch.setenv("GITHUB_TOKEN", "s3cret")

    _connect_and_resize()

    env = dict(pair.split("=", 1) for pair in fake_pty["env"].split("\0") if pair)
    assert "SUPABASE_SERVICE_KEY" not in env and "GITHUB_TOKEN" not in env
    assert "s3cret" not in fake_pty["env"]
    assert env["TERM"] == "xterm-256color"
    assert fake_pty["size"] == (100, 30)


def _supabase_stub(membership_rows, repo_rows=None):
    """Supabase double: token always valid; membership and repo rows configurable.

    Queries apply their .eq() and .not_.is_(col, "null") filters, so a pending
    invite (joined_at None) is only excluded if the code asks for that.
    """
    from unittest.mock import MagicMock
    from types import SimpleNamespace

    class Query:
        def __init__(self, rows):
            self.rows = rows
            self.filters = []
            self.negate = False

        def _chain(self, *a, **k):
            return self

        select = order = limit = maybe_single = single = _chain

        def eq(self, col, val):
            self.filters.append(lambda r: r.get(col) == val)
            return self

        @property
        def not_(self):
            self.negate = True
            return self

        def is_(self, col, val):
            assert val == "null"
            negate, self.negate = self.negate, False
            self.filters.append(lambda r: (r.get(col) is None) != negate)
            return self

        def execute(self):
            rows = [r for r in self.rows if all(f(r) for f in self.filters)]
            return SimpleNamespace(data=rows[0] if rows else None)

    tables = {
        "organization_members": membership_rows or [],
        "repositories": repo_rows or [],
    }
    sb = MagicMock()
    sb.auth.get_user.return_value = SimpleNamespace(
        user=SimpleNamespace(id="user-1", email="u@example.com")
    )
    sb.table.side_effect = lambda name: Query(tables[name])
    return sb


MEMBER = {"user_id": "user-1", "org_id": "org-1", "role": "member", "joined_at": "t"}


def test_terminal_websocket_rejects_user_without_org(monkeypatch):
    """A valid token is not enough; the HTTP routes require org membership too."""
    import agents

    monkeypatch.setattr(agents, "supabase", _supabase_stub([]), raising=False)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        _connect_and_resize()

    assert excinfo.value.code == 1008


def test_terminal_websocket_accepts_org_member(monkeypatch, fake_pty):
    """A token backed by an accepted, allowlisted membership is authorized."""
    import agents

    monkeypatch.setattr(agents, "supabase", _supabase_stub([MEMBER]), raising=False)

    _connect_and_resize()

    assert fake_pty["spawned"]


def test_terminal_rejects_repo_outside_callers_org(monkeypatch, tmp_path, fake_pty):
    """repo_url must be authorized before its clone becomes the shell's cwd.

    Regression guard: any tenant's clone named in repo_url was used as the
    working directory without a repository check.
    """
    import agents
    from workspace import get_safe_repo_dir

    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    get_safe_repo_dir("victim/private").mkdir(parents=True)
    victim_repo = {"id": 1, "github_installation_id": 2}
    victim_repo.update(org_id="org-victim", full_name="victim/private")
    sb = _supabase_stub([MEMBER], [victim_repo])
    monkeypatch.setattr(agents, "supabase", sb, raising=False)

    for repo_url in ("https://github.com/victim/private", "victim/private"):
        with pytest.raises(WebSocketDisconnect) as excinfo:
            _connect_and_resize(f"/api/terminal/ws?repo_url={repo_url}")
        assert excinfo.value.code == 1008

    assert not fake_pty["spawned"]


def test_terminal_rejects_unparseable_repo_url(monkeypatch, fake_pty):
    import agents

    monkeypatch.setattr(agents, "supabase", _supabase_stub([MEMBER]), raising=False)

    with pytest.raises(WebSocketDisconnect) as excinfo:
        _connect_and_resize("/api/terminal/ws?repo_url=../../etc")

    assert excinfo.value.code == 1008
    assert not fake_pty["spawned"]


@pytest.mark.parametrize(
    "repo_url",
    ["own/repo", "https://github.com/own/repo", "https://github.com/own/repo.git"],
)
def test_terminal_uses_authorized_repo_clone_as_cwd(
    monkeypatch, tmp_path, fake_pty, repo_url
):
    import os
    import agents
    from workspace import get_safe_repo_dir

    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path))
    clone = get_safe_repo_dir("own/repo")
    clone.mkdir(parents=True)
    own_repo = {"id": 1, "github_installation_id": 2}
    own_repo.update(org_id="org-1", full_name="own/repo")
    sb = _supabase_stub([MEMBER], [own_repo])
    monkeypatch.setattr(agents, "supabase", sb, raising=False)

    _connect_and_resize(f"/api/terminal/ws?repo_url={repo_url}")

    assert fake_pty["spawned"]
    assert fake_pty["cwd"] == os.path.realpath(clone)
