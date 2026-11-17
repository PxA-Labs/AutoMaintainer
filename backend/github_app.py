"""
GitHub App Authentication for AutoMaintainer
Replaces PAT-based authentication with GitHub App installation tokens.

Credential modes (env ``GITHUB_AUTH_MODE``):

``app``
    Multi-tenant mode. Every GitHub call for a repository uses a short-lived
    installation access token for *that repository's* App installation
    (``repositories.github_installation_id``). ``GITHUB_TOKEN`` is never used;
    an operation without an installation id is refused, not silently run with
    a global credential.
``pat``
    Explicit single-tenant fallback for self-hosting: everything runs as the
    ``GITHUB_TOKEN`` personal access token, whatever installation a repository
    belongs to. Never use it for a deployment that serves more than one tenant,
    because one PAT then reaches every tenant's repositories.

When ``GITHUB_AUTH_MODE`` is unset the mode is ``app`` if App credentials
(``GITHUB_APP_ID`` plus a private key) are configured, otherwise ``pat`` with
a warning logged at import.
"""

import os
import re
import base64
import hashlib
import hmac
import secrets
import threading
import time
import json
import asyncio
from urllib.parse import quote, urlencode
from datetime import datetime, timezone
import jwt
import httpx
from typing import Optional, Dict, Any
import github
from github import Github, GithubIntegration
import logging
from dataclasses import dataclass, field
from dotenv import load_dotenv

from automaintainer.context import active_config, secret_value

logger = logging.getLogger(__name__)

# agents.py imports this module before its own load_dotenv() runs, so load
# .env here too or the App credentials below would be read too early.
load_dotenv()

# GitHub App configuration
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_APP_PRIVATE_KEY = os.getenv("GITHUB_APP_PRIVATE_KEY")  # PEM format
GITHUB_APP_PRIVATE_KEY_PATH = os.getenv(
    "GITHUB_APP_PRIVATE_KEY_PATH"
)  # Path to PEM file
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")

GITHUB_API_URL = "https://api.github.com"

AUTH_MODE_APP = "app"
AUTH_MODE_PAT = "pat"

# Installation tokens live for one hour. A cached token is replaced once it is
# this close to expiry, so a git clone or API sequence started with it does
# not fail half-way.
TOKEN_REFRESH_MARGIN_SECONDS = 300

# Cache for installation tokens:
# (installation_id, repository or None, permissions key, App id)
#   -> (token, expires_at)
# The App id keeps two engines bound to different GitHub Apps (#186) from
# ever being handed each other's token for the same installation id.
_installation_token_cache: Dict[tuple, tuple[str, float]] = {}
_installation_token_lock = threading.Lock()


class GitHubAuthError(RuntimeError):
    """No acceptable GitHub credential exists for the requested operation."""


@dataclass(frozen=True)
class GitHubAppIdentity:
    """The GitHub App a credential acts for: its id and private key."""

    app_id: Optional[str]
    private_key: Optional[str] = field(default=None, repr=False)
    private_key_path: Optional[str] = None


def current_app_identity() -> GitHubAppIdentity:
    """The App of the engine running in this context (#186), else the App
    configured by GITHUB_APP_ID / GITHUB_APP_PRIVATE_KEY(_PATH)."""
    config = active_config()
    if config is not None:
        path = config.github_private_key_path
        return GitHubAppIdentity(
            app_id=secret_value(config.github_app_id),
            private_key=secret_value(config.github_private_key),
            private_key_path=str(path) if path is not None else None,
        )
    return GitHubAppIdentity(
        GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY, GITHUB_APP_PRIVATE_KEY_PATH
    )


def app_credentials_configured(app: Optional[GitHubAppIdentity] = None) -> bool:
    """True when an App id and a private key (inline or file) are configured."""
    app = app or current_app_identity()
    if not app.app_id:
        return False
    return bool(app.private_key) or bool(
        app.private_key_path and os.path.exists(app.private_key_path)
    )


def get_auth_mode() -> str:
    """Resolve GITHUB_AUTH_MODE (read on every call).

    An unrecognised value raises instead of guessing: falling back to ``pat``
    on a typo would silently put a multi-tenant deployment on one global
    credential.
    """
    config = active_config()
    if config is not None:
        if config.github_auth_mode is not None:
            return config.github_auth_mode
        return AUTH_MODE_APP if app_credentials_configured() else AUTH_MODE_PAT
    raw = (os.getenv("GITHUB_AUTH_MODE") or "").strip().lower()
    if raw in (AUTH_MODE_APP, AUTH_MODE_PAT):
        return raw
    if raw:
        raise GitHubAuthError(
            f"Invalid GITHUB_AUTH_MODE={raw!r}; expected 'app' or 'pat'"
        )
    return AUTH_MODE_APP if app_credentials_configured() else AUTH_MODE_PAT


def _log_auth_mode_at_startup() -> None:
    try:
        mode = get_auth_mode()
    except GitHubAuthError as e:
        logger.error("%s", e)
        return
    if mode == AUTH_MODE_PAT:
        logger.warning(
            "GitHub auth mode is 'pat': every repository is accessed with the "
            "single GITHUB_TOKEN personal access token. This is only safe for a "
            "single-tenant deployment; configure the GitHub App "
            "(GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY) for multi-tenant use."
        )


def load_private_key(app: Optional[GitHubAppIdentity] = None) -> str:
    """Load GitHub App private key from env or file."""
    app = app or current_app_identity()
    if app.private_key:
        return app.private_key
    if app.private_key_path and os.path.exists(app.private_key_path):
        with open(app.private_key_path, "r") as f:
            return f.read()
    raise ValueError(
        "GitHub App private key not configured. "
        "Set GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH"
    )


def get_github_integration() -> GithubIntegration:
    """Get authenticated GitHub Integration client."""
    app = current_app_identity()
    if not app.app_id:
        raise ValueError("GITHUB_APP_ID not configured")

    private_key = load_private_key(app)
    return GithubIntegration(
        auth=github.Auth.AppAuth(app_id=int(app.app_id), private_key=private_key)
    )


def generate_app_jwt(app: Optional[GitHubAppIdentity] = None) -> str:
    """Generate a JWT for GitHub App authentication."""
    app = app or current_app_identity()
    if not app.app_id:
        raise ValueError("GITHUB_APP_ID not configured")

    private_key = load_private_key(app)
    now = int(time.time())
    payload = {
        "iat": now - 60,  # Issued 60 seconds ago (clock skew tolerance)
        "exp": now + 600,  # Expires in 10 minutes (max allowed)
        "iss": app.app_id,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


# Permissions an agent run (and a repository route acting for one) needs:
# read/write code, open and merge pull requests, open and comment on issues,
# post the verification commit status. ``metadata: read`` is implied by any
# other permission but is listed so the token's scope is explicit.
RUN_TOKEN_PERMISSIONS: Dict[str, str] = {
    "contents": "write",
    "pull_requests": "write",
    "issues": "write",
    "statuses": "write",
    "metadata": "read",
}
# Added to a run token only when the installation has granted it.
# MERGE_POLICY=ci reads check runs; without this permission that read fails
# and the merge fails closed. Requesting a permission the installation does
# not have makes GitHub refuse the whole token, so it cannot be mandatory
# without breaking every run of installations that never granted it.
OPTIONAL_RUN_TOKEN_PERMISSIONS: Dict[str, str] = {"checks": "read"}
# Repository sync only lists the installation's repositories.
SYNC_TOKEN_PERMISSIONS: Dict[str, str] = {"metadata": "read"}

_PERMISSION_LEVELS = {"read": 1, "write": 2, "admin": 3}


class InstallationSuspendedError(GitHubAuthError):
    """The installation is suspended or uninstalled; no token is minted."""


def _app_api_client() -> httpx.Client:
    """HTTP client for App-JWT GitHub calls (patched in tests)."""
    return httpx.Client(base_url=GITHUB_API_URL, timeout=15.0)


def _app_headers(app: Optional[GitHubAppIdentity] = None) -> Dict[str, str]:
    # Called without an argument for the process App, so a test double of
    # generate_app_jwt that takes none keeps working.
    jwt_token = generate_app_jwt() if app is None else generate_app_jwt(app)
    return {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _installation_status_client():
    """The service-role Supabase client, or None when it is not configured.

    Imported lazily: agents imports this module at import time. Inside an
    engine run this is the engine's client (#186).
    """
    try:
        import agents
    except Exception:  # pragma: no cover - agents logs its own import errors
        return None
    return agents.get_supabase()


def _installation_row_suspended_at(installation_id: int) -> Optional[str]:
    """``suspended_at`` of the installation's row (set by the installation
    suspend/deleted webhooks), or None.

    A database error is logged and treated as "not suspended": GitHub's own
    ``suspended_at`` is checked right after and is authoritative, so the row
    is a fast local refusal rather than the only line of defence.
    """
    sb = _installation_status_client()
    if sb is None:
        return None
    try:
        result = (
            sb.table("github_installations")
            .select("suspended_at")
            .eq("id", installation_id)
            .limit(1)
            .execute()
        )
    except Exception as e:
        logger.warning(
            "Could not read installation %s status from the database: %s",
            installation_id,
            e,
        )
        return None
    rows = result.data if isinstance(getattr(result, "data", None), list) else []
    return rows[0].get("suspended_at") if rows else None


def _resolve_permissions(
    installation_id: int,
    required: Dict[str, str],
    optional: Dict[str, str],
    granted: Dict[str, str],
) -> Dict[str, str]:
    """The permission set to request: all of ``required`` (a clear error if
    the installation lacks any of it) plus whatever of ``optional`` the
    installation has granted."""

    def level(value: Optional[str]) -> int:
        return _PERMISSION_LEVELS.get(value or "", 0)

    missing = sorted(
        f"{name}:{needed}"
        for name, needed in required.items()
        if level(granted.get(name)) < level(needed)
    )
    if missing:
        raise GitHubAuthError(
            f"GitHub App installation {installation_id} has not granted the "
            f"permissions AutoMaintainer needs: {', '.join(missing)}. Accept "
            "the App's updated permissions in the installation settings."
        )
    resolved = dict(required)
    for name, wanted in optional.items():
        if level(granted.get(name)) >= level(wanted):
            resolved[name] = wanted
    return resolved


def _split_repository(repository: str) -> tuple[str, str]:
    owner, sep, name = (repository or "").partition("/")
    if not sep or not owner or not name or "/" in name:
        raise GitHubAuthError(f"Invalid repository name {repository!r}")
    return owner, name


def _suspended(installation_id: int, where: str) -> InstallationSuspendedError:
    return InstallationSuspendedError(
        f"GitHub App installation {installation_id} is {where}; no token was issued"
    )


def _mint_installation_token(
    installation_id: int,
    repository: Optional[str],
    required: Dict[str, str],
    optional: Dict[str, str],
    app: Optional[GitHubAppIdentity] = None,
) -> tuple[str, float]:
    """POST /app/installations/{id}/access_tokens with a narrowed scope.

    Refuses (InstallationSuspendedError) before any token exists when the
    installation row or GitHub (``suspended_at``, or 404 once uninstalled)
    says the installation is not active.
    """
    if _installation_row_suspended_at(installation_id):
        raise _suspended(installation_id, "suspended or uninstalled")

    with _app_api_client() as client:
        headers = _app_headers(app)
        resp = client.get(f"/app/installations/{installation_id}", headers=headers)
        if resp.status_code == 404:
            raise _suspended(installation_id, "uninstalled (not found on GitHub)")
        resp.raise_for_status()
        installation = resp.json()
        if installation.get("suspended_at"):
            raise _suspended(installation_id, "suspended on GitHub")

        body: Dict[str, Any] = {
            "permissions": _resolve_permissions(
                installation_id,
                required,
                optional,
                installation.get("permissions") or {},
            )
        }
        if repository:
            owner, name = _split_repository(repository)
            account = ((installation.get("account") or {}).get("login") or "").lower()
            if account and owner.lower() != account:
                raise GitHubAuthError(
                    f"Repository {repository} does not belong to the account of "
                    f"GitHub App installation {installation_id}"
                )
            body["repositories"] = [name]

        resp = client.post(
            f"/app/installations/{installation_id}/access_tokens",
            headers=headers,
            json=body,
        )
        # Suspension between the two calls: GitHub answers 403.
        if resp.status_code == 403 and "suspend" in resp.text.lower():
            raise _suspended(installation_id, "suspended on GitHub")
        # A repository the installation does not cover (not selected, or
        # removed from the installation) is a 422, not a server error.
        if resp.status_code == 422 and repository:
            raise GitHubAuthError(
                f"Repository {repository} is not accessible to GitHub App "
                f"installation {installation_id}"
            )
        resp.raise_for_status()
        data = resp.json()

    expires_raw = data.get("expires_at")
    if expires_raw:
        expires_at = datetime.fromisoformat(
            expires_raw.replace("Z", "+00:00")
        ).timestamp()
    else:
        expires_at = time.time() + 3600
    return data["token"], expires_at


def _token_cache_key(
    installation_id: int,
    repository: Optional[str],
    permissions: Optional[Dict[str, str]],
    app_id: Optional[str],
) -> tuple:
    # The installation id stays first: invalidate_installation_tokens and
    # callers that inspect the cache rely on k[0].
    return (
        installation_id,
        repository.lower() if repository else None,
        None if permissions is None else tuple(sorted(permissions.items())),
        app_id,
    )


def invalidate_installation_tokens(installation_id: int) -> None:
    """Drop every cached token of ``installation_id`` in this process.

    Called when the installation is suspended or deleted, from the webhook
    processor, which normally runs in the Celery worker. The cache is
    per-process: tokens already cached by the API server and by agent
    processes are not cleared and stay cached until they near expiry (at most
    about an hour). That is acceptable because GitHub itself rejects the
    tokens of a suspended or deleted installation, and every new mint
    re-checks the row and GitHub first.
    """
    installation_id = int(installation_id)
    with _installation_token_lock:
        for key in [k for k in _installation_token_cache if k[0] == installation_id]:
            del _installation_token_cache[key]


def get_installation_token_sync(
    installation_id: int,
    *,
    repository: Optional[str] = None,
    permissions: Optional[Dict[str, str]] = None,
    app: Optional[GitHubAppIdentity] = None,
) -> str:
    """Installation access token for ``installation_id``, cached until shortly
    before it expires (TOKEN_REFRESH_MARGIN_SECONDS).

    The token is narrowed to ``repository`` ("owner/name", when given) and to
    ``permissions`` (default: RUN_TOKEN_PERMISSIONS plus whichever
    OPTIONAL_RUN_TOKEN_PERMISSIONS the installation granted), so a leaked run
    token reaches one repository with only what the pipeline uses. Each scope
    is cached separately. A suspended or deleted installation raises
    InstallationSuspendedError and nothing is cached.

    Synchronous so PyGithub calls running in worker threads can refresh their
    credential. Two callers missing the cache at once may both mint a token;
    both are valid and the later one simply replaces the earlier.
    """
    installation_id = int(installation_id)
    # ``app``: the App the caller's credential was created for (an engine's,
    # #186); by default the App of this context.
    identity = app or current_app_identity()
    key = _token_cache_key(installation_id, repository, permissions, identity.app_id)
    with _installation_token_lock:
        cached = _installation_token_cache.get(key)
    if cached and time.time() < cached[1] - TOKEN_REFRESH_MARGIN_SECONDS:
        return cached[0]

    if permissions is None:
        required, optional = RUN_TOKEN_PERMISSIONS, OPTIONAL_RUN_TOKEN_PERMISSIONS
    else:
        required, optional = dict(permissions), {}
    try:
        token, expires_at = _mint_installation_token(
            installation_id, repository, required, optional, app
        )
    except Exception as e:
        logger.error(f"Failed to get installation token for {installation_id}: {e}")
        raise
    with _installation_token_lock:
        _installation_token_cache[key] = (token, expires_at)
    return token


async def get_installation_token(
    installation_id: int,
    *,
    repository: Optional[str] = None,
    permissions: Optional[Dict[str, str]] = None,
) -> str:
    """
    Get a valid installation access token for a GitHub App installation.
    Uses caching to avoid repeated API calls; minting runs off the event loop.
    """
    return await asyncio.to_thread(
        lambda: get_installation_token_sync(
            installation_id, repository=repository, permissions=permissions
        )
    )


class GitHubCredentials:
    """The credential one repository operation (or one agent run) acts with.

    Either an App installation, whose token is minted and refreshed on demand,
    or (``pat`` mode only) the single-tenant GITHUB_TOKEN. ``token()`` and
    ``client()`` can block on a network call when the cached token is stale.
    """

    def __init__(
        self,
        *,
        installation_id: Optional[int] = None,
        pat: Optional[str] = None,
        repository: Optional[str] = None,
        app: Optional[GitHubAppIdentity] = None,
    ):
        if (installation_id is None) == (pat is None):
            raise ValueError("Pass exactly one of installation_id or pat")
        self.installation_id = installation_id
        # The App the token is minted for when it is not the process App (an
        # engine's, #186). Bound here because token() also runs in threads.
        self._app = app
        # App mode: the token is scoped to this repository when it is known.
        self.repository = repository
        self._pat = pat
        self._client: Optional[Github] = None
        self._client_token: Optional[str] = None

    @property
    def mode(self) -> str:
        return AUTH_MODE_PAT if self._pat is not None else AUTH_MODE_APP

    def token(self) -> str:
        if self._pat is not None:
            return self._pat
        if self._app is not None:
            return get_installation_token_sync(
                self.installation_id, repository=self.repository, app=self._app
            )
        return get_installation_token_sync(
            self.installation_id, repository=self.repository
        )

    def client(self) -> Github:
        """PyGithub client for the current token, rebuilt after a refresh."""
        token = self.token()
        if self._client is None or token != self._client_token:
            self._client = Github(auth=github.Auth.Token(token))
            self._client_token = token
        return self._client

    def __repr__(self) -> str:  # never include the token
        if self._pat is not None:
            return "GitHubCredentials(mode='pat')"
        return f"GitHubCredentials(installation_id={self.installation_id})"


def credentials_for_installation(
    installation_id: Optional[int], *, purpose: str, repository: Optional[str] = None
) -> GitHubCredentials:
    """Pick the credential for an operation on a repository of ``installation_id``.

    ``app`` mode: that installation's token, with RUN_TOKEN_PERMISSIONS and,
    when ``repository`` ("owner/name") is given, access to that repository
    only. Without an installation id the
    only remaining credential would be the global PAT, so this logs at ERROR
    and raises GitHubAuthError instead. ``pat`` mode: GITHUB_TOKEN.
    ``purpose`` only labels log lines.
    """
    mode = get_auth_mode()
    if mode == AUTH_MODE_APP:
        if not installation_id:
            logger.error(
                "REFUSING to fall back to the GITHUB_TOKEN PAT for %s: "
                "GITHUB_AUTH_MODE is 'app' and no GitHub App installation id "
                "is known for the repository.",
                purpose,
            )
            raise GitHubAuthError(
                "No GitHub App installation is linked to this repository"
            )
        if not app_credentials_configured():
            raise GitHubAuthError(
                "GITHUB_AUTH_MODE is 'app' but GitHub App credentials are not configured"
            )
        engine_app = current_app_identity() if active_config() else None
        return GitHubCredentials(
            installation_id=int(installation_id),
            repository=repository,
            app=engine_app,
        )

    config = active_config()
    if config is not None:
        pat = secret_value(config.github_token)
    else:
        pat = os.getenv("GITHUB_TOKEN")
    if not pat:
        raise GitHubAuthError("GITHUB_AUTH_MODE is 'pat' but GITHUB_TOKEN is not set")
    return GitHubCredentials(pat=pat)


async def get_github_client_for_installation(
    installation_id: int, *, permissions: Optional[Dict[str, str]] = None
) -> Github:
    """Get an authenticated PyGithub client for a specific installation."""
    token = await get_installation_token(installation_id, permissions=permissions)
    return Github(auth=github.Auth.Token(token))


async def get_installation_repositories(installation_id: int) -> list[Dict[str, Any]]:
    """List repositories accessible to a GitHub App installation.

    Uses a metadata-only token: listing needs nothing more.
    """
    client = await get_github_client_for_installation(
        installation_id, permissions=SYNC_TOKEN_PERMISSIONS
    )
    try:
        repos = []
        for repo in client.get_installation_repos():
            repos.append(
                {
                    "id": repo.id,
                    "full_name": repo.full_name,
                    "name": repo.name,
                    "owner_login": repo.owner.login,
                    "owner_type": repo.owner.type,
                    "private": repo.private,
                    "default_branch": repo.default_branch,
                    "description": repo.description,
                    "language": repo.language,
                    "topics": repo.get_topics(),
                    "avatar_url": repo.owner.avatar_url,
                    "html_url": repo.html_url,
                    "archived": repo.archived,
                    "disabled": repo.disabled,
                    "pushed_at": repo.pushed_at.isoformat() if repo.pushed_at else None,
                }
            )
        return repos
    finally:
        client.close()


async def sync_installation_repositories(
    supabase_client, installation_id: int, org_id: str
) -> int:
    """
    Sync repositories from GitHub App installation to database.
    Returns count of repositories synced.

    A repository row that already belongs to a different organization is
    skipped and logged, never moved. The upsert keys on the GitHub repo id, so
    without this check a second installation covering the same repository
    would reassign another tenant's repository (and its runs) to this org.

    The ownership read and the upsert are two statements, so another org's
    sync can insert the row in between. The database closes that race: the
    repositories_org_immutable trigger (supabase_schema.sql) rejects any
    update that changes org_id, so the losing upsert fails, is logged as a
    conflict and is not counted. The read above stays as the cheap common
    path and keeps the expected case out of the error log.
    """
    repos = await get_installation_repositories(installation_id)
    synced = 0

    active = [r for r in repos if not (r["archived"] or r["disabled"])]
    owners = await _existing_repository_orgs(supabase_client, [r["id"] for r in active])

    for repo_data in active:
        owner_org = owners.get(repo_data["id"])
        if owner_org is not None and str(owner_org) != str(org_id):
            logger.warning(
                "Not syncing repo %s (id %s) from installation %s into org %s: "
                "it already belongs to org %s",
                repo_data["full_name"],
                repo_data["id"],
                installation_id,
                org_id,
                owner_org,
            )
            continue

        try:
            repository_record = {
                "id": repo_data["id"],
                "org_id": org_id,
                "github_installation_id": installation_id,
                "full_name": repo_data["full_name"],
                "name": repo_data["name"],
                "owner_login": repo_data["owner_login"],
                "owner_type": repo_data["owner_type"],
                "private": repo_data["private"],
                "default_branch": repo_data["default_branch"],
                "description": repo_data["description"],
                "language": repo_data["language"],
                "topics": repo_data["topics"],
                "avatar_url": repo_data["avatar_url"],
                "html_url": repo_data["html_url"],
                "archived": repo_data["archived"],
                "disabled": repo_data["disabled"],
                "pushed_at": repo_data["pushed_at"],
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }
            await asyncio.to_thread(
                lambda: supabase_client.table("repositories")
                .upsert(repository_record, on_conflict="id")
                .execute()
            )
            synced += 1
        except Exception as e:
            if _is_repository_org_conflict(e):
                logger.warning(
                    "Not syncing repo %s (id %s) from installation %s into org %s: "
                    "another org linked it concurrently (repository_org_immutable)",
                    repo_data["full_name"],
                    repo_data["id"],
                    installation_id,
                    org_id,
                )
                continue
            logger.error(f"Failed to sync repo {repo_data['full_name']}: {e}")

    return synced


# Raised by the repositories_org_immutable trigger (supabase_schema.sql).
REPOSITORY_ORG_CONFLICT_MARKER = "repository_org_immutable"


def _is_repository_org_conflict(exc: Exception) -> bool:
    """True if ``exc`` is the database refusing to move a repository to
    another org. postgrest-py's APIError carries the message in ``message``
    (and in str()); the check accepts either."""
    text = f"{getattr(exc, 'message', '') or ''} {exc}"
    return REPOSITORY_ORG_CONFLICT_MARKER in text


async def _existing_repository_orgs(supabase_client, repo_ids: list) -> dict:
    """Map repository id -> org_id for those of ``repo_ids`` that have a row."""
    if not repo_ids:
        return {}
    result = await asyncio.to_thread(
        lambda: supabase_client.table("repositories")
        .select("id, org_id")
        .in_("id", repo_ids)
        .execute()
    )
    rows = result.data if isinstance(getattr(result, "data", None), list) else []
    return {row["id"]: row["org_id"] for row in rows}


# --- Installation ownership (claim) -----------------------------------------

# /user/installations is paginated; an account that can see more than
# MAX_PAGES * 100 installations of this App is not a realistic caller.
_USER_INSTALLATIONS_MAX_PAGES = 10


class InstallationClaimError(Exception):
    """A claim was rejected; ``status_code``/``detail`` are safe to return."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _user_api_client() -> httpx.AsyncClient:
    """HTTP client for user-token GitHub calls (patched in tests)."""
    return httpx.AsyncClient(base_url=GITHUB_API_URL, timeout=15.0)


async def _user_get(client: httpx.AsyncClient, path: str, token: str, **params):
    return await client.get(
        path,
        params=params or None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


async def verify_installation_claim(
    user_token: str,
    installation_id: int,
    expected_github_user_id: Optional[int] = None,
) -> dict:
    """Prove that the holder of ``user_token`` controls ``installation_id``.

    ``user_token`` is the caller's GitHub user-to-server token (the Supabase
    GitHub provider_token). Checks, all against GitHub rather than anything
    the client asserts:

    1. the installation is one of this App's installations the user can access
       (GET /user/installations);
    2. its account matches the user: for a User account, the account login is
       the user's own login; for an Organization, the user is an active admin
       (owner) of it (GET /user/memberships/orgs/{org}). Repository access
       alone is not enough, since /user/installations also lists installations
       where the user is merely a collaborator.

    When ``expected_github_user_id`` is given (the install callback), the
    token must also belong to that GitHub user, so a token for someone else
    cannot complete a flow another user started.

    Returns the github_installations columns derived from GitHub's response
    (org_id is added by the caller). Raises InstallationClaimError otherwise.
    """
    async with _user_api_client() as client:
        me = await _user_get(client, "/user", user_token)
        if me.status_code in (401, 403):
            raise InstallationClaimError(401, "GitHub user token is invalid or expired")
        me.raise_for_status()
        me_data = me.json()
        user_login = (me_data.get("login") or "").lower()
        if expected_github_user_id is not None and me_data.get("id") != int(
            expected_github_user_id
        ):
            raise InstallationClaimError(
                403,
                "The GitHub account that installed the App is not the one "
                "that started the installation",
            )

        installation = None
        for page in range(1, _USER_INSTALLATIONS_MAX_PAGES + 1):
            resp = await _user_get(
                client, "/user/installations", user_token, per_page=100, page=page
            )
            if resp.status_code in (401, 403):
                raise InstallationClaimError(
                    401, "GitHub user token cannot list App installations"
                )
            resp.raise_for_status()
            batch = resp.json().get("installations") or []
            installation = next(
                (i for i in batch if i.get("id") == int(installation_id)), None
            )
            if installation is not None or len(batch) < 100:
                break

        if installation is None:
            raise InstallationClaimError(
                403, "This GitHub App installation is not accessible to your account"
            )

        account = installation.get("account") or {}
        account_type = account.get("type")
        account_login = account.get("login") or ""
        if account_type == "User":
            if account_login.lower() != user_login:
                raise InstallationClaimError(
                    403, "This installation belongs to a different GitHub user"
                )
        elif account_type == "Organization":
            membership = await _user_get(
                client, f"/user/memberships/orgs/{account_login}", user_token
            )
            data = membership.json() if membership.status_code == 200 else {}
            if data.get("state") != "active" or data.get("role") != "admin":
                raise InstallationClaimError(
                    403,
                    "You must be an owner of the GitHub organization that "
                    "installed the App",
                )
        else:
            raise InstallationClaimError(400, "Unsupported installation account type")

    return {
        "id": int(installation["id"]),
        "account_login": account_login,
        "account_type": account_type,
        "account_avatar_url": account.get("avatar_url"),
        "target_type": installation.get("target_type") or account_type,
        "repository_selection": installation.get("repository_selection") or "all",
        "permissions": installation.get("permissions") or {},
        "events": installation.get("events") or [],
        "suspended_at": installation.get("suspended_at"),
    }


# --- GitHub App install flow (setup URL callback) ---------------------------
#
# An authenticated org owner/admin asks for an install URL with only their
# Supabase session. It carries a ``state`` that is HMAC-signed, expires, is
# single-use, and is bound to the AutoMaintainer user, their org and their
# GitHub user id. That id is read server-side from the user's GitHub identity
# in Supabase Auth (created when Supabase completed a GitHub OAuth sign-in
# for this user), not from anything the browser sends, so the dashboard needs
# no GitHub token (Supabase drops ``provider_token`` on session refresh).
# With "Request user authorization (OAuth) during installation" enabled,
# GitHub sends the browser back with that state, the installation id and an
# OAuth ``code``. The callback checks and consumes the state, exchanges the
# code for the installer's user-to-server token and runs
# verify_installation_claim with it, so the installation id in the URL is
# never trusted by itself and ownership is proven against GitHub at link
# time. Binding the GitHub user id stops a "link my org to your
# installation" CSRF: a state minted by one user fails when a different
# GitHub user completes the install.

SETUP_STATE_TTL_SECONDS = 600
_SETUP_STATE_VERSION = "v1"
_SETUP_STATE_CONTEXT = b"automaintainer/github-app-setup-state/v1"
GITHUB_OAUTH_TOKEN_URL = "https://github.com/login/oauth/access_token"


class SetupStateError(Exception):
    """The setup ``state`` is missing, forged, malformed or expired."""


class SetupStateExpired(SetupStateError):
    """A correctly signed state whose lifetime has passed."""


class SetupStateReplayed(SetupStateError):
    """A correctly signed, unexpired state that was already used once."""


def install_flow_configured() -> bool:
    """Slug (for the install URL) and OAuth client credentials are set."""
    return all(
        (os.getenv(k) or "").strip()
        for k in ("GITHUB_APP_SLUG", "GITHUB_APP_CLIENT_ID", "GITHUB_APP_CLIENT_SECRET")
    )


def _setup_state_key() -> bytes:
    # Derived from the App's OAuth client secret with a fixed context, so no
    # extra secret has to be configured and the raw client secret is never
    # used directly as an HMAC key. Rotating the client secret invalidates
    # states still in flight (at most SETUP_STATE_TTL_SECONDS old).
    secret = (os.getenv("GITHUB_APP_CLIENT_SECRET") or "").strip()
    if not secret:
        raise SetupStateError("GitHub App client secret is not configured")
    return hmac.new(secret.encode(), _SETUP_STATE_CONTEXT, hashlib.sha256).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def create_setup_state(
    user_id: str, org_id: str, github_user_id: int, now: Optional[float] = None
) -> str:
    """A signed, expiring state bound to (user_id, org_id, github_user_id)."""
    payload = {
        "u": str(user_id),
        "o": str(org_id),
        "g": int(github_user_id),
        "exp": int((time.time() if now is None else now) + SETUP_STATE_TTL_SECONDS),
        "n": secrets.token_urlsafe(12),
    }
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    signed = f"{_SETUP_STATE_VERSION}.{body}"
    sig = hmac.new(_setup_state_key(), signed.encode(), hashlib.sha256).digest()
    return f"{signed}.{_b64(sig)}"


def verify_setup_state(state: Optional[str], now: Optional[float] = None) -> dict:
    """Return the state's payload ({u, o, g, exp, n}) or raise SetupStateError.

    The signature is checked (constant time) before the payload is parsed.
    This does not consume the state; the setup callback calls
    consume_setup_state once the state has verified.
    """
    if not state or len(state) > 1024:
        raise SetupStateError("missing state")
    parts = state.split(".")
    if len(parts) != 3 or parts[0] != _SETUP_STATE_VERSION:
        raise SetupStateError("malformed state")
    signed = f"{parts[0]}.{parts[1]}"
    expected = hmac.new(_setup_state_key(), signed.encode(), hashlib.sha256).digest()
    try:
        sig = _unb64(parts[2])
    except Exception:
        raise SetupStateError("malformed state")
    if not hmac.compare_digest(sig, expected):
        raise SetupStateError("bad state signature")
    try:
        payload = json.loads(_unb64(parts[1]))
        if not isinstance(payload, dict):
            raise ValueError
        payload["g"] = int(payload["g"])
        payload["exp"] = int(payload["exp"])
        if not payload.get("u") or not payload.get("o"):
            raise ValueError
    except Exception:
        raise SetupStateError("malformed state")
    if payload["exp"] <= (time.time() if now is None else now):
        raise SetupStateExpired("expired state")
    return payload


def build_install_url(state: str) -> str:
    """https://github.com/apps/<GITHUB_APP_SLUG>/installations/new?state=..."""
    slug = (os.getenv("GITHUB_APP_SLUG") or "").strip()
    return (
        f"https://github.com/apps/{quote(slug, safe='')}/installations/new?"
        f"{urlencode({'state': state})}"
    )


# Used state nonces when REDIS_URL is not configured (single-process local
# development): nonce -> expiry (epoch seconds).
_used_setup_nonces: Dict[str, float] = {}
_used_setup_nonces_lock = threading.Lock()


def _claim_setup_nonce(nonce: str, ttl: int, now: float) -> bool:
    """Record ``nonce`` as used; False when it already was.

    Redis (SET NX with the state's remaining lifetime) when REDIS_URL is set,
    so every API instance shares it; otherwise an in-process map, which only
    covers one process. A Redis error propagates, so the caller fails closed.
    """
    if os.getenv("REDIS_URL"):
        from redis_locks import get_redis_client

        key = f"automaintainer:github-setup-state:{nonce}"
        return bool(get_redis_client().set(key, "1", nx=True, ex=ttl))
    with _used_setup_nonces_lock:
        for used, expires in list(_used_setup_nonces.items()):
            if expires <= now:
                del _used_setup_nonces[used]
        if nonce in _used_setup_nonces:
            return False
        _used_setup_nonces[nonce] = now + ttl
        return True


def consume_setup_state(payload: dict, now: Optional[float] = None) -> None:
    """Mark a verified state (a verify_setup_state payload) as used.

    Raises SetupStateReplayed when it was used before. The nonce is kept for
    the state's remaining lifetime; after that the state is expired anyway.
    """
    now = time.time() if now is None else now
    nonce = payload.get("n")
    if not isinstance(nonce, str) or not nonce:
        raise SetupStateError("malformed state")
    ttl = max(1, int(payload["exp"] - now))
    if not _claim_setup_nonce(nonce, ttl, now):
        raise SetupStateReplayed("state already used")


_GITHUB_ID_RE = re.compile(r"[0-9]{1,19}")


def github_user_id_from_identities(identities) -> Optional[int]:
    """The GitHub user id of a Supabase Auth user's GitHub identity.

    ``identities`` is ``User.identities`` from the Supabase admin API (objects
    or dicts). Supabase creates an identity only after completing that
    provider's OAuth flow, and users cannot edit identities (unlike
    ``user_metadata``), so its id is GitHub's numeric user id as GitHub
    reported it. Returns None when there is no GitHub identity, or more than
    one distinct one.
    """

    def field(obj, name):
        return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)

    found = set()
    for identity in identities or ():
        if field(identity, "provider") != "github":
            continue
        data = field(identity, "identity_data")
        data = data if isinstance(data, dict) else {}
        for raw in (field(identity, "id"), data.get("provider_id"), data.get("sub")):
            text = "" if raw is None else str(raw)
            if _GITHUB_ID_RE.fullmatch(text) and int(text) > 0:
                found.add(int(text))
                break
    return found.pop() if len(found) == 1 else None


def _oauth_client() -> httpx.AsyncClient:
    """HTTP client for the OAuth code exchange (patched in tests)."""
    return httpx.AsyncClient(timeout=15.0)


async def exchange_oauth_code(code: str) -> str:
    """Exchange an install-time OAuth ``code`` for a user-to-server token.

    Raises InstallationClaimError (401) when GitHub rejects the code; codes
    are single-use and short-lived, so a replayed or stale one fails here.
    """
    async with _oauth_client() as client:
        resp = await client.post(
            GITHUB_OAUTH_TOKEN_URL,
            data={
                "client_id": (os.getenv("GITHUB_APP_CLIENT_ID") or "").strip(),
                "client_secret": (os.getenv("GITHUB_APP_CLIENT_SECRET") or "").strip(),
                "code": code,
            },
            headers={"Accept": "application/json"},
        )
    if resp.status_code != 200:
        raise InstallationClaimError(502, "GitHub OAuth code exchange failed")
    try:
        token = (resp.json() or {}).get("access_token")
    except ValueError:
        token = None
    if not token:
        # GitHub answers 200 with {"error": "bad_verification_code"} for an
        # invalid, expired or already-used code.
        raise InstallationClaimError(401, "GitHub rejected the authorization code")
    return token


class WebhookSignatureError(ValueError):
    """X-Hub-Signature-256 is missing or does not match the body.

    A ValueError subclass so existing callers keep working; POST
    /webhooks/github maps it to 401 and every other ValueError (a signed but
    malformed delivery) to 400.
    """


async def handle_github_webhook(
    raw_body: bytes, headers: Dict[str, str], supabase_client
) -> Dict[str, Any]:
    """
    Process incoming GitHub webhook event.
    Validates the signature against the raw request body and queues for processing.
    """
    # HTTP header names are case-insensitive, and the ASGI server lower-cases
    # them: main.py passes dict(request.headers), so a lookup of
    # "X-Hub-Signature-256" on that plain dict missed every real delivery.
    headers = {str(k).lower(): v for k, v in headers.items()}

    # Verify webhook signature over the exact bytes GitHub signed.
    if not GITHUB_WEBHOOK_SECRET:
        raise ValueError("GitHub webhook secret is not configured")
    signature = headers.get("x-hub-signature-256", "")
    if not verify_webhook_signature(raw_body, signature):
        raise WebhookSignatureError("Invalid webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"Invalid webhook payload: {e}")

    event_type = headers.get("x-github-event")
    delivery_id = headers.get("x-github-delivery")
    if not event_type or not delivery_id:
        raise ValueError("Missing required GitHub webhook headers")

    # Extract installation ID from payload
    installation_id = payload.get("installation", {}).get("id")
    if not installation_id:
        return {"status": "ignored", "reason": "No installation ID in payload"}

    # Get org_id from installation
    result = await asyncio.to_thread(
        lambda: supabase_client.table("github_installations")
        .select("org_id")
        .eq("id", installation_id)
        .single()
        .execute()
    )

    if not result.data:
        return {
            "status": "ignored",
            "reason": f"Installation {installation_id} not registered",
        }

    org_id = result.data["org_id"]

    existing = await asyncio.to_thread(
        lambda: supabase_client.table("webhook_events")
        .select("id, status")
        .eq("github_delivery_id", delivery_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        return {
            "status": "duplicate",
            "event_type": event_type,
            "delivery_id": delivery_id,
        }

    # Store webhook event for async processing
    webhook_data = {
        "org_id": org_id,
        "github_installation_id": installation_id,
        "github_delivery_id": delivery_id,
        "event_type": event_type,
        "action": payload.get("action"),
        "payload": payload,
        "status": "pending",
    }

    await asyncio.to_thread(
        lambda: supabase_client.table("webhook_events").insert(webhook_data).execute()
    )

    return {"status": "queued", "event_type": event_type, "delivery_id": delivery_id}


def verify_webhook_signature(raw_body: bytes, signature_header: str) -> bool:
    """
    Verify GitHub webhook signature (HMAC SHA256).

    GitHub computes X-Hub-Signature-256 over the exact bytes of the request
    body, so verification must use those raw bytes - re-serializing a parsed
    dict would produce a different byte stream (key order, unicode escaping,
    float formatting) and fail or, worse, allow crafted duplicates.
    """
    import hmac
    import hashlib

    if not raw_body or not signature_header.startswith("sha256="):
        return False

    if not GITHUB_WEBHOOK_SECRET:
        return False

    expected_sig = signature_header[7:]  # Remove "sha256="
    # compare_digest raises TypeError on non-ASCII str; a forged header must
    # be a plain mismatch, not a 500.
    if not expected_sig.isascii():
        return False
    computed_sig = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256
    ).hexdigest()

    return hmac.compare_digest(computed_sig, expected_sig)


async def get_github_app_client() -> Github:
    """Get a GitHub client authenticated as the App (for management operations)."""
    integration = get_github_integration()
    # App-level client for managing installations, etc.
    jwt_token = generate_app_jwt()
    return Github(jwt_token)


# Export commonly used functions
__all__ = [
    "get_github_integration",
    "get_installation_token",
    "get_installation_token_sync",
    "get_github_client_for_installation",
    "get_auth_mode",
    "credentials_for_installation",
    "GitHubCredentials",
    "GitHubAuthError",
    "InstallationSuspendedError",
    "invalidate_installation_tokens",
    "RUN_TOKEN_PERMISSIONS",
    "verify_installation_claim",
    "InstallationClaimError",
    "get_installation_repositories",
    "sync_installation_repositories",
    "handle_github_webhook",
    "WebhookSignatureError",
    "get_github_app_client",
    "generate_app_jwt",
    "verify_webhook_signature",
]

_log_auth_mode_at_startup()
