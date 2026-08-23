"""
GitHub App Authentication for AutoMaintainer
Replaces PAT-based authentication with GitHub App installation tokens.
"""
import os
import time
import jwt
import httpx
from typing import Optional, Dict, Any
from github import Github, GithubIntegration
import logging

logger = logging.getLogger(__name__)

# GitHub App configuration
GITHUB_APP_ID = os.getenv("GITHUB_APP_ID")
GITHUB_APP_PRIVATE_KEY = os.getenv("GITHUB_APP_PRIVATE_KEY")  # PEM format
GITHUB_APP_PRIVATE_KEY_PATH = os.getenv("GITHUB_APP_PRIVATE_KEY_PATH")  # Path to PEM file
GITHUB_WEBHOOK_SECRET = os.getenv("GITHUB_WEBHOOK_SECRET")

# Cache for installation tokens (installation_id -> (token, expires_at))
_installation_token_cache: Dict[int, tuple[str, float]] = {}


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
        auth=github.Auth.AppAuth(
            app_id=int(GITHUB_APP_ID),
            private_key=private_key
        )
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


async def get_installation_token(installation_id: int) -> str:
    """
    Get a valid installation access token for a GitHub App installation.
    Uses caching to avoid repeated API calls.
    """
    # Check cache
    if installation_id in _installation_token_cache:
        token, expires_at = _installation_token_cache[installation_id]
        if time.time() < expires_at - 60:  # 60 second buffer
            return token
    
    # Fetch new token
    integration = get_github_integration()
    try:
        auth = integration.get_installation_auth(installation_id)
        token = auth.token
        expires_at = auth.expires_at.timestamp() if auth.expires_at else time.time() + 3600
        
        _installation_token_cache[installation_id] = (token, expires_at)
        return token
    except Exception as e:
        logger.error(f"Failed to get installation token for {installation_id}: {e}")
        raise


async def get_github_client_for_installation(installation_id: int) -> Github:
    """Get an authenticated PyGithub client for a specific installation."""
    token = await get_installation_token(installation_id)
    return Github(token)


async def get_installation_repositories(installation_id: int) -> list[Dict[str, Any]]:
    """List repositories accessible to a GitHub App installation."""
    client = await get_github_client_for_installation(installation_id)
    try:
        repos = []
        for repo in client.get_installation_repos():
            repos.append({
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
            })
        return repos
    finally:
        client.close()


async def sync_installation_repositories(
    supabase_client,
    installation_id: int,
    org_id: str
) -> int:
    """
    Sync repositories from GitHub App installation to database.
    Returns count of repositories synced.
    """
    repos = await get_installation_repositories(installation_id)
    synced = 0
    
    for repo_data in repos:
        if repo_data["archived"] or repo_data["disabled"]:
            continue
            
        try:
            await supabase_client.table("repositories").upsert({
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
                "synced_at": time.time(),
            }, on_conflict="id").execute()
            synced += 1
        except Exception as e:
            logger.error(f"Failed to sync repo {repo_data['full_name']}: {e}")
    
    return synced


async def handle_github_webhook(
    payload: Dict[str, Any],
    headers: Dict[str, str],
    supabase_client
) -> Dict[str, Any]:
    """
    Process incoming GitHub webhook event.
    Validates signature and queues for processing.
    """
    # Verify webhook signature
    if GITHUB_WEBHOOK_SECRET:
        signature = headers.get("X-Hub-Signature-256", "")
        if not verify_webhook_signature(payload, signature):
            raise ValueError("Invalid webhook signature")
    
    event_type = headers.get("X-GitHub-Event", "unknown")
    delivery_id = headers.get("X-GitHub-Delivery", "unknown")
    
    # Extract installation ID from payload
    installation_id = payload.get("installation", {}).get("id")
    if not installation_id:
        return {"status": "ignored", "reason": "No installation ID in payload"}
    
    # Get org_id from installation
    result = await supabase_client.table("github_installations") \
        .select("org_id") \
        .eq("id", installation_id) \
        .single() \
        .execute()
    
    if not result.data:
        return {"status": "ignored", "reason": f"Installation {installation_id} not registered"}
    
    org_id = result.data["org_id"]
    
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
    
    await supabase_client.table("webhook_events").insert(webhook_data).execute()
    
    return {"status": "queued", "event_type": event_type, "delivery_id": delivery_id}


def verify_webhook_signature(payload: Dict[str, Any], signature_header: str) -> bool:
    """Verify GitHub webhook signature (HMAC SHA256)."""
    import hmac
    import hashlib
    
    if not signature_header.startswith("sha256="):
        return False
    
    expected_sig = signature_header[7:]  # Remove "sha256="
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    computed_sig = hmac.new(
        GITHUB_WEBHOOK_SECRET.encode(),
        payload_bytes,
        hashlib.sha256
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
    "get_github_client_for_installation",
    "get_installation_repositories",
    "sync_installation_repositories",
    "handle_github_webhook",
    "get_github_app_client",
    "generate_app_jwt",
    "verify_webhook_signature",
]