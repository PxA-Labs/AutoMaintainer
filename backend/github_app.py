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
import threading
import time
import json
import asyncio
from datetime import datetime, timezone
import jwt
import httpx
from typing import Optional, Dict, Any
import github
from github import Github, GithubIntegration
import logging
from dotenv import load_dotenv

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

# Cache for installation tokens (installation_id -> (token, expires_at))
_installation_token_cache: Dict[int, tuple[str, float]] = {}
_installation_token_lock = threading.Lock()


class GitHubAuthError(RuntimeError):
    """No acceptable GitHub credential exists for the requested operation."""


def app_credentials_configured() -> bool:
    """True when an App id and a private key (inline or file) are configured."""
    if not GITHUB_APP_ID:
        return False
    return bool(GITHUB_APP_PRIVATE_KEY) or bool(
        GITHUB_APP_PRIVATE_KEY_PATH and os.path.exists(GITHUB_APP_PRIVATE_KEY_PATH)
    )


def get_auth_mode() -> str:
    """Resolve GITHUB_AUTH_MODE (read on every call).

    An unrecognised value raises instead of guessing: falling back to ``pat``
    on a typo would silently put a multi-tenant deployment on one global
    credential.
    """
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


def load_private_key() -> str:
    """Load GitHub App private key from env or file."""
    if GITHUB_APP_PRIVATE_KEY:
        return GITHUB_APP_PRIVATE_KEY
    if GITHUB_APP_PRIVATE_KEY_PATH and os.path.exists(GITHUB_APP_PRIVATE_KEY_PATH):
        with open(GITHUB_APP_PRIVATE_KEY_PATH, "r") as f:
            return f.read()
    raise ValueError(
        "GitHub App private key not configured. "
        "Set GITHUB_APP_PRIVATE_KEY or GITHUB_APP_PRIVATE_KEY_PATH"
    )


def get_github_integration() -> GithubIntegration:
    """Get authenticated GitHub Integration client."""
    if not GITHUB_APP_ID:
        raise ValueError("GITHUB_APP_ID not configured")

    private_key = load_private_key()
    return GithubIntegration(
        auth=github.Auth.AppAuth(app_id=int(GITHUB_APP_ID), private_key=private_key)
    )


def generate_app_jwt() -> str:
    """Generate a JWT for GitHub App authentication."""
    if not GITHUB_APP_ID:
        raise ValueError("GITHUB_APP_ID not configured")

    private_key = load_private_key()
    now = int(time.time())
    payload = {
        "iat": now - 60,  # Issued 60 seconds ago (clock skew tolerance)
        "exp": now + 600,  # Expires in 10 minutes (max allowed)
        "iss": GITHUB_APP_ID,
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


def get_installation_token_sync(installation_id: int) -> str:
    """Installation access token for ``installation_id``, cached until shortly
    before it expires (TOKEN_REFRESH_MARGIN_SECONDS).

    Synchronous so PyGithub calls running in worker threads can refresh their
    credential. Two callers missing the cache at once may both mint a token;
    both are valid and the later one simply replaces the earlier.
    """
    installation_id = int(installation_id)
    with _installation_token_lock:
        cached = _installation_token_cache.get(installation_id)
    if cached and time.time() < cached[1] - TOKEN_REFRESH_MARGIN_SECONDS:
        return cached[0]

    integration = get_github_integration()
    try:
        auth = integration.get_installation_auth(installation_id)
    except Exception as e:
        logger.error(f"Failed to get installation token for {installation_id}: {e}")
        raise
    token = auth.token
    expires_at = auth.expires_at.timestamp() if auth.expires_at else time.time() + 3600
    with _installation_token_lock:
        _installation_token_cache[installation_id] = (token, expires_at)
    return token


async def get_installation_token(installation_id: int) -> str:
    """
    Get a valid installation access token for a GitHub App installation.
    Uses caching to avoid repeated API calls; minting runs off the event loop.
    """
    return await asyncio.to_thread(get_installation_token_sync, installation_id)


class GitHubCredentials:
    """The credential one repository operation (or one agent run) acts with.

    Either an App installation, whose token is minted and refreshed on demand,
    or (``pat`` mode only) the single-tenant GITHUB_TOKEN. ``token()`` and
    ``client()`` can block on a network call when the cached token is stale.
    """

    def __init__(
        self, *, installation_id: Optional[int] = None, pat: Optional[str] = None
    ):
        if (installation_id is None) == (pat is None):
            raise ValueError("Pass exactly one of installation_id or pat")
        self.installation_id = installation_id
        self._pat = pat
        self._client: Optional[Github] = None
        self._client_token: Optional[str] = None

    @property
    def mode(self) -> str:
        return AUTH_MODE_PAT if self._pat is not None else AUTH_MODE_APP

    def token(self) -> str:
        if self._pat is not None:
            return self._pat
        return get_installation_token_sync(self.installation_id)

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
    installation_id: Optional[int], *, purpose: str
) -> GitHubCredentials:
    """Pick the credential for an operation on a repository of ``installation_id``.

    ``app`` mode: that installation's token. Without an installation id the
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
        return GitHubCredentials(installation_id=int(installation_id))

    pat = os.getenv("GITHUB_TOKEN")
    if not pat:
        raise GitHubAuthError("GITHUB_AUTH_MODE is 'pat' but GITHUB_TOKEN is not set")
    return GitHubCredentials(pat=pat)


async def get_github_client_for_installation(installation_id: int) -> Github:
    """Get an authenticated PyGithub client for a specific installation."""
    token = await get_installation_token(installation_id)
    return Github(auth=github.Auth.Token(token))


async def get_installation_repositories(installation_id: int) -> list[Dict[str, Any]]:
    """List repositories accessible to a GitHub App installation."""
    client = await get_github_client_for_installation(installation_id)
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

    Known gap: the ownership read and the upsert are two statements, so a row
    inserted by another org between them could still be overwritten. Closing
    that needs a database-side guard (a unique (installation, repo) key or an
    immutable org_id), which epic #412 tracks.
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
            logger.error(f"Failed to sync repo {repo_data['full_name']}: {e}")

    return synced


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


async def verify_installation_claim(user_token: str, installation_id: int) -> dict:
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

    Returns the github_installations columns derived from GitHub's response
    (org_id is added by the caller). Raises InstallationClaimError otherwise.
    """
    async with _user_api_client() as client:
        me = await _user_get(client, "/user", user_token)
        if me.status_code in (401, 403):
            raise InstallationClaimError(401, "GitHub user token is invalid or expired")
        me.raise_for_status()
        user_login = (me.json().get("login") or "").lower()

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
        raise ValueError("Invalid webhook signature")

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
    "verify_installation_claim",
    "InstallationClaimError",
    "get_installation_repositories",
    "sync_installation_repositories",
    "handle_github_webhook",
    "get_github_app_client",
    "generate_app_jwt",
    "verify_webhook_signature",
]

_log_auth_mode_at_startup()
