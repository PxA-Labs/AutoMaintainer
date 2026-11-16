from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    BackgroundTasks,
    HTTPException,
    Depends,
    Header,
    Query,
    Request,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from github import Github, GithubException, UnknownObjectException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from typing import Optional, List, Annotated, Literal
from workspace import get_safe_repo_dir, get_safe_target_path, get_base_workspace_dir
import asyncio
import hashlib
import json
import uuid
import httpx
import os
import sys
import platform
import subprocess
import re
import logging
import threading
import time
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from urllib.parse import urlsplit

# Celery integration
from celery_app import celery_app, redis_configured
from tasks import run_agent_loop_task, cancel_run_task, cleanup_stale_runs
from github_app import (
    SETUP_STATE_TTL_SECONDS,
    SetupStateError,
    SetupStateExpired,
    build_install_url,
    create_setup_state,
    exchange_oauth_code,
    github_user_id_for_token,
    install_flow_configured,
    verify_setup_state,
    AUTH_MODE_APP,
    GitHubAuthError,
    InstallationClaimError,
    credentials_for_installation,
    get_auth_mode,
    handle_github_webhook,
    verify_installation_claim,
)
from quotas import assist_limiter
from webhook_processor import process_pending_events, process_webhook_events_task

# Observability
from observability import (
    init_observability,
    instrument_fastapi,
    shutdown_observability,
    get_tracer,
    record_agent_run_complete,
    record_llm_tokens,
    record_llm_request,
    record_github_api_call,
    set_celery_queue_depth,
    record_celery_task_duration,
    inject_trace_context,
    check_observability_health,
    setup_structured_logging,
)

logger = logging.getLogger(__name__)

gitnexus_process = None


# --- Pydantic Models ---


class StartRequest(BaseModel):
    repo_name: str
    target_issue: Optional[int] = None
    mode: str = "autonomous"  # autonomous, targeted_issue, code_review, refactor


class StopRequest(BaseModel):
    run_id: Optional[str] = None


class FileUpdateRequest(BaseModel):
    file_path: str
    content: str
    commit_message: Optional[str] = None
    # Branch to commit to. Omitted: the caller's IDE working branch (see
    # _ide_working_branch). The default branch and protected branches are
    # always refused (#139).
    branch: Optional[str] = None


class FileCreateRequest(BaseModel):
    file_path: str
    content: str = ""
    is_dir: bool = False
    commit_message: Optional[str] = None
    branch: Optional[str] = None


def internal_error(status_code: int, public_message: str, exc: BaseException):
    """Log ``exc`` server-side and return an HTTPException that does not echo it.

    Exception text from PyGithub, PostgREST or Celery can carry hostnames,
    query details, tokens embedded in URLs and stack fragments. Clients get a
    generic message plus an opaque error id that correlates with the log line.
    """
    error_id = uuid.uuid4().hex[:12]
    logger.error(
        f"{public_message} [error_id={error_id}]: {exc}",
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return HTTPException(
        status_code=status_code, detail=f"{public_message} (error id: {error_id})"
    )


def _is_production() -> bool:
    return os.getenv("ENVIRONMENT", "development").strip().lower() == "production"


def dev_bypass_enabled() -> bool:
    """AUTH_DEV_BYPASS, refused outright when ENVIRONMENT=production.

    The bypass trusts every request as a fixed dev user. A single stray env var
    on a production service must not turn that into an open API, so the flag
    is ignored there (and the app also refuses to start; see lifespan).
    """
    if os.getenv("AUTH_DEV_BYPASS", "").strip().lower() != "true":
        return False
    if _is_production():
        logger.critical(
            "AUTH_DEV_BYPASS=true is set with ENVIRONMENT=production; ignoring it. "
            "Remove AUTH_DEV_BYPASS from the production environment."
        )
        return False
    logger.warning("AUTH_DEV_BYPASS is enabled - all requests are trusted!")
    return True


def _dev_bypass_user() -> dict:
    return {
        "user_id": os.getenv(
            "AUTH_DEV_USER_ID", "00000000-0000-0000-0000-000000000001"
        ),
        "org_id": os.getenv("AUTH_DEV_ORG_ID", "00000000-0000-0000-0000-000000000002"),
        "role": os.getenv("AUTH_DEV_ROLE", "owner"),
        "email": "dev@localhost",
    }


def _fetch_membership(supabase_client, user_id: str):
    """The caller's earliest ACCEPTED organization membership.

    Rows with joined_at NULL are pending invites. They must not grant access,
    matching the schema's RLS helpers (user_org_ids / user_is_org_admin), which
    already require joined_at IS NOT NULL.
    """
    return (
        supabase_client.table("organization_members")
        .select("org_id, role")
        .eq("user_id", user_id)
        .not_.is_("joined_at", "null")
        .order("joined_at")
        .limit(1)
        .maybe_single()
        .execute()
    )


def _row_or_none(result) -> Optional[dict]:
    """The row from a ``.limit(1).maybe_single().execute()`` query, or None.

    ``.single()`` makes PostgREST answer 406 when no row matches, which
    postgrest-py raises as APIError, so a missing repo/run/membership surfaced
    as a 500. ``.maybe_single()`` instead returns ``None`` (not a response
    object) for zero rows; ``.limit(1)`` keeps it from raising if a filter
    unexpectedly matches several rows.
    """
    if result is None:
        return None
    return getattr(result, "data", None) or None


# Org roles allowed to change repository contents or start/stop runs.
# 'viewer' is read-only. Unknown or missing roles fail closed.
WRITE_ROLES = frozenset({"owner", "admin", "member"})
ADMIN_ROLES = frozenset({"owner", "admin"})


def _github_identity(user):
    """The user's ``github`` identity, or None."""
    for identity in getattr(user, "identities", None) or []:
        if getattr(identity, "provider", None) == "github":
            return identity
    return None


def github_login_of(user) -> Optional[str]:
    """The GitHub login linked to a Supabase auth user, or None.

    Read from the user's ``github`` identity, which Supabase fills from
    GitHub OAuth at sign-in, never from ``user_metadata``, which the user
    can change through the auth API. It is the login as of the last sign-in,
    so it can be stale; the trusted-trigger gate (trust.py) decides by
    github_id_of and only logs this one.
    """
    identity = _github_identity(user)
    data = getattr(identity, "identity_data", None) or {}
    login = data.get("user_name") or data.get("preferred_username")
    return login if isinstance(login, str) and login else None


def _numeric_id(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value) or None
    return None


def github_id_of(user) -> Optional[int]:
    """The numeric GitHub user id linked to a Supabase auth user, or None.

    Supabase Auth (GoTrue) keys an identity by ``provider_id``, which its
    GitHub provider sets to the GitHub user id (the ``sub`` claim) and
    exposes as the identity's ``id``; ``identity_data.sub`` holds the same
    value. Neither can be changed through the auth API (``user_metadata``
    can, so it is never read). Unlike the login, the id survives a rename;
    the trusted-trigger gate (trust.py) checks the caller by it.
    """
    identity = _github_identity(user)
    if identity is None:
        return None
    found = _numeric_id(getattr(identity, "id", None))
    if found is None:
        data = getattr(identity, "identity_data", None) or {}
        found = _numeric_id(data.get("sub"))
    return found


# --- Dependency: Get current user/org from Supabase Auth ---
async def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> dict:
    """
    Validate the Supabase access token from the Authorization header and
    resolve the caller's identity and organization membership.

    Every org-scoped endpoint depends on this, so an unauthenticated request
    can never reach data belonging to another tenant.
    """
    from agents import supabase

    if not supabase:
        raise HTTPException(
            status_code=503,
            detail="Authentication backend (Supabase) is not configured",
        )

    # Explicit opt-in for local development only; never enabled by default.
    if dev_bypass_enabled():
        return _dev_bypass_user()

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or malformed Authorization header (expected 'Bearer <token>')",
        )

    access_token = authorization.split(" ", 1)[1].strip()

    try:
        user_response = await asyncio.to_thread(supabase.auth.get_user, access_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired access token")

    user = getattr(user_response, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired access token")

    # Resolve the caller's primary (accepted) organization membership.
    org_id = None
    role = None
    try:
        membership = await asyncio.to_thread(_fetch_membership, supabase, user.id)
        if membership and membership.data:
            org_id = membership.data["org_id"]
            role = membership.data.get("role")
    except Exception as e:
        logger.warning(f"Failed to resolve org membership for {user.id}: {e}")

    if not org_id:
        raise HTTPException(
            status_code=403,
            detail="No organization membership found. Complete onboarding first.",
        )

    return {
        "user_id": user.id,
        "org_id": org_id,
        "role": role,
        "email": user.email,
        "github_login": github_login_of(user),
        "github_id": github_id_of(user),
    }


async def get_org_id(user: dict = Depends(get_current_user)) -> str:
    return user["org_id"]


async def get_user_id(user: dict = Depends(get_current_user)) -> str:
    return user["user_id"]


async def get_org_role(user: dict = Depends(get_current_user)) -> Optional[str]:
    return user.get("role")


async def require_write_role(role: Optional[str] = Depends(get_org_role)) -> str:
    """Reject org members whose role is read-only ('viewer').

    Write routes commit with the server's GitHub credentials, so membership
    alone is not enough.
    """
    if role not in WRITE_ROLES:
        raise HTTPException(
            status_code=403, detail="Your organization role does not allow changes"
        )
    return role


# --- Repository validation helper ---
async def validate_repo_access(
    repo_name: str, org_id: str, user_id: str, supabase_client
) -> tuple[int, int]:
    """
    Validate user has access to repo and return (repository_id, github_installation_id).
    """
    # Check if repo exists in org's tracked repositories
    result = await asyncio.to_thread(
        lambda: supabase_client.table("repositories")
        .select("id, github_installation_id")
        .eq("org_id", org_id)
        .eq("full_name", repo_name)
        .limit(1)
        .maybe_single()
        .execute()
    )
    repo = _row_or_none(result)

    if not repo:
        raise HTTPException(
            status_code=404,
            detail=f"Repository {repo_name} not found in organization. Please sync repositories first.",
        )

    return repo["id"], repo["github_installation_id"]


async def authenticate_websocket(websocket: WebSocket) -> Optional[dict]:
    """Validate a Supabase access token presented on a WebSocket handshake.

    Browsers cannot set an Authorization header when opening a WebSocket, so
    the token travels as a subprotocol: ``new WebSocket(url, ["bearer", tok])``.
    A query parameter is deliberately not used -- query strings are recorded in
    proxy and server access logs.

    Returns the caller's identity, or None if the handshake is not authorized.
    """
    from agents import supabase

    # The terminal additionally requires the user id to be on
    # TERMINAL_ADMIN_USER_IDS, so the bypass alone never grants a shell.
    if dev_bypass_enabled():
        return _dev_bypass_user()

    protocols = websocket.scope.get("subprotocols") or []
    if len(protocols) < 2 or protocols[0] != "bearer":
        return None

    access_token = protocols[1].strip()
    if not access_token or not supabase:
        return None

    try:
        user_response = await asyncio.to_thread(supabase.auth.get_user, access_token)
    except Exception:
        return None

    user = getattr(user_response, "user", None)
    if not user:
        return None

    # A valid token is not sufficient. get_current_user also requires an
    # organization_members row and 403s without one; the terminal must not be
    # weaker than the HTTP routes. Pending invites (joined_at NULL) do not count.
    try:
        membership = await asyncio.to_thread(_fetch_membership, supabase, user.id)
    except Exception as e:
        logger.warning(f"Failed to resolve org membership for {user.id}: {e}")
        return None

    if not membership or not membership.data:
        return None

    return {
        "user_id": user.id,
        "org_id": membership.data["org_id"],
        "role": membership.data.get("role"),
        "email": user.email,
    }


async def require_repo_access(
    repo_name: str,
    request: Request,
    org_id: str = Depends(get_org_id),
    user_id: str = Depends(get_user_id),
) -> str:
    """Authenticate the caller and authorize them for ``repo_name``.

    Declared as a dependency so it works on both ``def`` and ``async def``
    handlers: FastAPI resolves ``repo_name`` from the path and awaits this
    before the endpoint runs. Returns the caller's org_id so handlers can
    scope filesystem and database access to the tenant.

    The repository id found by the same lookup is kept on the request for
    _repo_dir_for_request, so keying the clone by id costs no second query.
    """
    from agents import supabase as agents_supabase

    if not agents_supabase:
        raise HTTPException(status_code=503, detail="Database not configured")

    repository_id, _ = await validate_repo_access(
        repo_name, org_id, user_id, agents_supabase
    )
    _remember_repository_id(request, repo_name, repository_id)
    return org_id


def _remember_repository_id(request: Request, repo_name: str, repository_id) -> None:
    ids = getattr(request.state, "repository_ids", None)
    if ids is None:
        ids = {}
        request.state.repository_ids = ids
    ids[repo_name] = repository_id


def _repo_dir_for_request(request: Request, repo_name: str) -> Path:
    """The local clone of ``repo_name`` for a route guarded by require_repo_access.

    Keyed by the repository id that require_repo_access looked up (org-scoped,
    ``repositories.id``), which is the id the agent run is given
    (``runs.repository_id``), so the IDE reads the clone the agent wrote.
    require_repo_access always finds an id or refuses the request, so the
    name-keyed fallback is only reached when that dependency did not run
    (tests that override it).
    """
    ids = getattr(request.state, "repository_ids", None) or {}
    return get_safe_repo_dir(repo_name, ids.get(repo_name))


async def require_repo_write_access(
    org_id: str = Depends(require_repo_access),
    role: str = Depends(require_write_role),
) -> str:
    """require_repo_access plus a role that may change repository contents."""
    return org_id


@asynccontextmanager
async def lifespan(app: FastAPI):
    global gitnexus_process

    # Refuse to boot a production service with the auth bypass set. Requests
    # would ignore it anyway (dev_bypass_enabled), but failing the deploy makes
    # the misconfiguration impossible to miss.
    if _is_production() and os.getenv("AUTH_DEV_BYPASS", "").strip().lower() == "true":
        raise RuntimeError(
            "AUTH_DEV_BYPASS=true is not allowed when ENVIRONMENT=production"
        )

    # Initialize observability
    setup_structured_logging()
    init_observability(
        service_name="automaintainer-backend",
        service_version="1.0.0",
        environment=os.getenv("ENVIRONMENT", "development"),
        otlp_endpoint=os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
        enable_console_export=os.getenv("ENVIRONMENT") != "production",
        enable_prometheus=True,
    )
    instrument_fastapi(app)

    # Start the GitNexus MCP server in the background
    try:
        cmd = (
            ["gitnexus.cmd", "serve"]
            if platform.system() == "Windows"
            else ["gitnexus", "serve"]
        )
        gitnexus_process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print("GitNexus server started on port 4747")
    except Exception as e:
        print(f"Failed to start GitNexus: {e}")

    # Check Supabase connection on startup
    from agents import supabase

    if supabase:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: supabase.table("runs").select("id").limit(1).execute()
                ),
                timeout=3.0,
            )
            print("Supabase connection established successfully.")
        except Exception as e:
            logger.critical(
                f"CRITICAL WARNING: Supabase connection failed on startup. "
                f"The database might be paused, unreachable, or undergoing maintenance. "
                f"Error: {e}"
            )
    else:
        logger.warning("Supabase is not configured. Persistence will be disabled.")

    yield

    # Shutdown observability
    shutdown_observability()

    if gitnexus_process:
        gitnexus_process.terminate()
        print("GitNexus server stopped")


app = FastAPI(title="AutoMaintainer Backend", lifespan=lifespan)

DEFAULT_ALLOWED_ORIGINS = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
)

# Vercel preview deployments get a fresh subdomain per build, so they are matched
# by regex instead of being enumerated in ALLOWED_ORIGINS.
DEFAULT_ALLOWED_ORIGIN_REGEX = r"^https:\/\/.*\.vercel\.app$"


def parse_allowed_origins(configured_origins: str | None) -> list[str]:
    """Return safe, normalized CORS origins from a comma-separated setting."""
    candidates = list(DEFAULT_ALLOWED_ORIGINS)
    if configured_origins:
        candidates.extend(origin.strip() for origin in configured_origins.split(","))

    origins = []
    for candidate in candidates:
        if not candidate:
            continue
        parsed = urlsplit(candidate)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "ALLOWED_ORIGINS must contain comma-separated http(s) origins "
                f"without paths or wildcards: {candidate!r}"
            )
        normalized = f"{parsed.scheme}://{parsed.netloc}"
        if normalized not in origins:
            origins.append(normalized)
    return origins


# ALLOWED_ORIGINS is the name used by render.yaml; CORS_ORIGINS is kept as an
# alias so existing deployments keep working.
ALLOWED_ORIGINS = parse_allowed_origins(
    os.getenv("ALLOWED_ORIGINS") or os.getenv("CORS_ORIGINS")
)

# Allow the Next.js frontend to connect to this API. Origins are exact matches;
# wildcard origins are intentionally rejected because credentials are enabled.
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=os.getenv("CORS_ORIGIN_REGEX", DEFAULT_ALLOWED_ORIGIN_REGEX),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _process_webhook_inline(sb, delivery_id: str) -> None:
    """Background-task body when no broker is configured (local development)."""
    try:
        process_pending_events(sb, delivery_id=delivery_id)
    except Exception:
        logger.exception("Inline processing of webhook %s failed", delivery_id)


async def _schedule_webhook_processing(
    delivery_id: str, background_tasks: BackgroundTasks, sb
) -> None:
    """Hand a stored delivery to webhook_processor.

    The row is already persisted, so a failure to enqueue is logged rather
    than reported to GitHub: the periodic sweep processes pending rows.
    Without REDIS_URL there is no worker, so the event is processed in this
    process after the response is sent, like tasks.start_agent_task does.
    """
    if not redis_configured():
        background_tasks.add_task(_process_webhook_inline, sb, delivery_id)
        return
    try:
        await asyncio.to_thread(
            process_webhook_events_task.apply_async,
            kwargs={"delivery_id": delivery_id},
            queue="github_sync",
        )
    except Exception:
        logger.warning(
            "Could not enqueue webhook %s; the periodic sweep will process it",
            delivery_id,
            exc_info=True,
        )


@app.post("/webhooks/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    """Receive and persist signed GitHub App webhook deliveries, then
    enqueue their processing."""
    from agents import supabase as agents_supabase

    if not os.getenv("GITHUB_WEBHOOK_SECRET"):
        raise HTTPException(
            status_code=503, detail="GitHub webhook secret is not configured"
        )
    if not agents_supabase:
        raise HTTPException(status_code=503, detail="Database not configured")

    raw_body = await request.body()
    try:
        result = await handle_github_webhook(
            raw_body,
            dict(request.headers),
            agents_supabase,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("GitHub webhook processing failed")
        raise HTTPException(status_code=500, detail="Failed to queue GitHub webhook")

    # Only newly stored rows; duplicates were enqueued on first delivery.
    if result.get("status") == "queued":
        await _schedule_webhook_processing(
            result["delivery_id"], background_tasks, agents_supabase
        )
    return result


@app.get("/healthz")
async def healthz():
    """Process liveness probe - never touches external services."""
    return {"status": "healthy"}


@app.get("/healthz/supabase")
async def healthz_supabase():
    from agents import supabase

    if not supabase:
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured. Please check SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables.",
        )
    try:
        await asyncio.to_thread(
            lambda: supabase.table("runs").select("id").limit(1).execute()
        )
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        # This probe is public: neither the exception text nor the Supabase
        # host (which identifies the project) goes into the response.
        raise internal_error(503, "Supabase connection failed", e)


@app.get("/healthz/observability")
async def healthz_observability():
    """Health check for observability components."""
    return check_observability_health()


class InlineAssistSelection(BaseModel):
    startLine: int
    startColumn: int
    endLine: int
    endColumn: int


class InlineAssistRequest(BaseModel):
    repo_name: Optional[str] = None
    file_path: str
    prompt: str
    selected_code: str
    prefix_code: str = ""
    suffix_code: str = ""
    selection: InlineAssistSelection


# --- New Celery-based endpoints ---

AGENT_QUEUE = "agent_runs"
WORKER_PING_TIMEOUT_SECONDS = float(os.getenv("WORKER_PING_TIMEOUT_SECONDS", "1.0"))
WORKER_PING_CACHE_SECONDS = float(os.getenv("WORKER_PING_CACHE_SECONDS", "5.0"))
_worker_probe = {"checked_at": float("-inf"), "available": False}
_worker_probe_lock = threading.Lock()


def _probe_agent_worker() -> bool:
    """True if some live Celery worker consumes the agent_runs queue.

    Uses active_queues rather than ping: on Render the maintenance worker
    (maintenance,github_sync) answers a ping even when no agent worker runs,
    and runs would then sit in the queue forever.
    """
    try:
        replies = celery_app.control.inspect(
            timeout=WORKER_PING_TIMEOUT_SECONDS
        ).active_queues()
    except Exception as exc:
        logger.warning("Celery worker probe failed: %s", exc)
        return False
    return any(
        queue.get("name") == AGENT_QUEUE
        for queues in (replies or {}).values()
        for queue in queues or ()
    )


def _agent_worker_available_cached() -> bool:
    """The worker probe, cached so /start costs at most one broadcast per window.

    Both answers are cached: a newly started worker is seen within
    WORKER_PING_CACHE_SECONDS, and a burst of /start calls while no worker
    runs does not become a burst of broadcasts. The lock makes concurrent
    callers share one probe; it is a threading lock because this runs in the
    thread pool and must not be tied to one event loop.
    """
    with _worker_probe_lock:
        if time.monotonic() - _worker_probe["checked_at"] >= WORKER_PING_CACHE_SECONDS:
            _worker_probe["available"] = _probe_agent_worker()
            _worker_probe["checked_at"] = time.monotonic()
        return _worker_probe["available"]


async def agent_worker_available() -> bool:
    return await asyncio.to_thread(_agent_worker_available_cached)


@app.post("/start")
async def start_agents(
    req: StartRequest,
    org_id: str = Depends(get_org_id),
    user_id: str = Depends(get_user_id),
    _role: str = Depends(require_write_role),
    user: dict = Depends(get_current_user),
):
    """
    Start an agent run via Celery task queue.
    Returns immediately with run_id for polling.
    """
    # The caller's linked GitHub login goes to the worker as the run's
    # trigger (source "api"); MERGE_POLICY=ci/auto merge only when it can
    # write to the repository (trust.py). A caller without one gets no
    # automatic merge.
    from agents import supabase as agents_supabase

    sb = agents_supabase
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Validate repo access and get IDs
    try:
        repository_id, github_installation_id = await validate_repo_access(
            req.repo_name, org_id, user_id, sb
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_error(500, "Repository validation failed", e)

    # Without a worker the task would wait in the broker indefinitely and the
    # run would stay "queued". Checked before the run row is created.
    if not await agent_worker_available():
        raise HTTPException(
            status_code=503,
            detail="No agent worker is available to run this job. "
            "Start a Celery worker on the 'agent_runs' queue and try again.",
            headers={"Retry-After": str(int(WORKER_PING_CACHE_SECONDS) or 1)},
        )

    # Create run record in database
    run_id = str(uuid.uuid4())

    run_data = {
        "id": run_id,
        "org_id": org_id,
        "user_id": user_id,
        "repository_id": repository_id,
        "github_installation_id": github_installation_id,
        "repo_name": req.repo_name,
        "target_issue_number": req.target_issue,
        "mode": req.mode,
        "status": "queued",
        "queued_at": datetime.utcnow().isoformat(),
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }

    try:
        await asyncio.to_thread(lambda: sb.table("runs").insert(run_data).execute())
    except Exception as e:
        raise internal_error(500, "Failed to create run record", e)

    # Queue the Celery task
    try:
        task = run_agent_loop_task.apply_async(
            args=[
                run_id,
                req.repo_name,
                org_id,
                user_id,
                repository_id,
                github_installation_id,
            ],
            kwargs={
                "target_issue_number": req.target_issue,
                "mode": req.mode,
                "trigger_login": user.get("github_login"),
                "trigger_github_id": user.get("github_id"),
                "trigger_source": "api",
            },
            queue="agent_runs",
        )

        # Store Celery task ID for tracking
        await asyncio.to_thread(
            lambda: sb.table("runs")
            .update({"celery_task_id": task.id})
            .eq("id", run_id)
            .execute()
        )

    except Exception as e:
        err_msg = str(e)
        # Mark run as failed if queueing fails
        await asyncio.to_thread(
            lambda msg=err_msg: sb.table("runs")
            .update(
                {
                    "status": "failed",
                    "error_message": f"Failed to queue task: {msg}",
                    "completed_at": datetime.utcnow().isoformat(),
                }
            )
            .eq("id", run_id)
            .execute()
        )
        raise internal_error(500, "Failed to queue agent run", e)

    return {
        "status": "queued",
        "run_id": run_id,
        "celery_task_id": task.id,
        "message": "Agent run queued. Poll /status/{run_id} for updates.",
    }


@app.get("/status/{run_id}")
async def get_run_status(run_id: str, org_id: str = Depends(get_org_id)):
    """Get current status of a run."""
    from agents import supabase as agents_supabase

    sb = agents_supabase
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")

    result = await asyncio.to_thread(
        lambda: sb.table("runs")
        .select("*")
        .eq("id", run_id)
        .eq("org_id", org_id)
        .limit(1)
        .maybe_single()
        .execute()
    )
    run = _row_or_none(result)

    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # If still queued/running, check Celery task status
    celery_task_id = run.get("celery_task_id")
    if celery_task_id and run["status"] in ("queued", "running"):
        try:
            celery_result = celery_app.AsyncResult(celery_task_id)
            run["celery_status"] = celery_result.status
            run["celery_result"] = (
                celery_result.result if celery_result.ready() else None
            )
        except Exception:
            pass

    return run


@app.get("/runs")
async def list_runs(
    org_id: str = Depends(get_org_id),
    status: Optional[str] = None,
    # Bounded so a caller cannot ask PostgREST for an absurd or negative range.
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100_000),
):
    """List runs for the current organization."""
    from agents import supabase as agents_supabase

    sb = agents_supabase
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")

    query = (
        sb.table("runs").select("*").eq("org_id", org_id).order("created_at", desc=True)
    )

    if status:
        query = query.eq("status", status)

    result = await asyncio.to_thread(
        lambda: query.range(offset, offset + limit - 1).execute()
    )

    return {
        "runs": result.data or [],
        "total": len(result.data or []),
        "limit": limit,
        "offset": offset,
    }


@app.get("/runs/{run_id}/verifications")
async def list_run_verifications(run_id: uuid.UUID, org_id: str = Depends(get_org_id)):
    """Verification audit rows of a run in the caller's organization, newest
    first (docs/VERIFICATION.md, "Audit trail")."""
    from agents import supabase as agents_supabase

    sb = agents_supabase
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")

    run_key = str(run_id)
    # Same check as /status: another org's run is indistinguishable from a
    # missing one.
    owned = await asyncio.to_thread(
        lambda: sb.table("runs")
        .select("id")
        .eq("id", run_key)
        .eq("org_id", org_id)
        .limit(1)
        .maybe_single()
        .execute()
    )
    if not _row_or_none(owned):
        raise HTTPException(status_code=404, detail="Run not found")

    # org_id again so a row can never leak even if it disagreed with its run.
    result = await asyncio.to_thread(
        lambda: sb.table("verification_results")
        .select("*")
        .eq("run_id", run_key)
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    return {"run_id": run_key, "verifications": result.data or []}


@app.get("/runs/{run_id}/decisions")
async def list_run_merge_decisions(
    run_id: uuid.UUID, org_id: str = Depends(get_org_id)
):
    """Merge decision audit rows of a run in the caller's organization, newest
    first (docs/VERIFICATION.md, "Merge decisions audit")."""
    from agents import supabase as agents_supabase

    sb = agents_supabase
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")

    run_key = str(run_id)
    # Same check as /status and /verifications: another org's run is
    # indistinguishable from a missing one.
    owned = await asyncio.to_thread(
        lambda: sb.table("runs")
        .select("id")
        .eq("id", run_key)
        .eq("org_id", org_id)
        .limit(1)
        .maybe_single()
        .execute()
    )
    if not _row_or_none(owned):
        raise HTTPException(status_code=404, detail="Run not found")

    result = await asyncio.to_thread(
        lambda: sb.table("merge_decisions")
        .select("*")
        .eq("run_id", run_key)
        .eq("org_id", org_id)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
    )
    return {"run_id": run_key, "decisions": result.data or []}


# --- Per-repository merge policy (epic #411, merge_policy.py) ---------------


class MergePolicyUpdate(BaseModel):
    # Required (no default); null means "inherit MERGE_POLICY".
    merge_policy: Optional[Literal["human", "ci", "auto"]]


async def _org_repository(sb, repository_id: int, org_id: str) -> dict:
    """The caller's org's repositories row (id, full_name, merge_policy), or
    404 -- another org's repository is indistinguishable from a missing one."""
    found = await asyncio.to_thread(
        lambda: sb.table("repositories")
        .select("id, full_name, merge_policy")
        .eq("id", repository_id)
        .eq("org_id", org_id)
        .limit(1)
        .maybe_single()
        .execute()
    )
    repository = _row_or_none(found)
    if not repository:
        raise HTTPException(status_code=404, detail="Repository not found")
    return repository


def _merge_policy_view(repository: dict, role: Optional[str]) -> dict:
    import merge_policy

    ceiling = merge_policy.deployment_ceiling()
    setting = repository.get("merge_policy")
    return {
        "repository_id": repository["id"],
        "full_name": repository.get("full_name"),
        "setting": setting,
        "ceiling": ceiling,
        "effective": merge_policy.effective(ceiling, setting),
        "allowed": merge_policy.allowed_settings(ceiling),
        "can_edit": role in ADMIN_ROLES,
    }


@app.get("/repositories/{repository_id}/merge-policy")
async def get_repository_merge_policy(
    repository_id: int, user: dict = Depends(get_current_user)
):
    """A repository's merge policy: its stored setting (null = inherit), the
    deployment ceiling (MERGE_POLICY), the effective policy and the settings
    an admin may choose. Any member of the repository's org may read it."""
    from agents import supabase as agents_supabase

    sb = agents_supabase
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")
    repository = await _org_repository(sb, repository_id, user["org_id"])
    return _merge_policy_view(repository, user.get("role"))


@app.put("/repositories/{repository_id}/merge-policy")
async def put_repository_merge_policy(
    repository_id: int,
    payload: MergePolicyUpdate,
    user: dict = Depends(get_current_user),
):
    """Set a repository's merge policy (org owners and admins only). A
    repository may only be stricter than or equal to MERGE_POLICY: a looser
    value is refused with 422 instead of being stored as an intent that never
    takes effect. Written with the service role; clients cannot write the
    column themselves (RLS, migration *_repository_merge_policy.sql)."""
    import merge_policy

    from agents import supabase as agents_supabase

    sb = agents_supabase
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")
    # Checked before the lookup, so a non-admin learns nothing about ids.
    if user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only organization owners and admins can change the merge policy",
        )
    org_id = user["org_id"]
    await _org_repository(sb, repository_id, org_id)

    ceiling = merge_policy.deployment_ceiling()
    if not merge_policy.is_allowed(payload.merge_policy, ceiling):
        raise HTTPException(
            status_code=422,
            detail=(
                f"merge_policy {payload.merge_policy!r} is looser than this "
                f"deployment's MERGE_POLICY ({ceiling}); allowed: "
                + ", ".join(merge_policy.allowed_settings(ceiling))
                + " or null (inherit)"
            ),
        )

    updated = await asyncio.to_thread(
        lambda: sb.table("repositories")
        .update({"merge_policy": payload.merge_policy})
        .eq("id", repository_id)
        .eq("org_id", org_id)
        .execute()
    )
    rows = getattr(updated, "data", None)
    if not isinstance(rows, list) or not rows:
        # Deleted between the lookup and the update.
        raise HTTPException(status_code=404, detail="Repository not found")
    logger.info(
        "Merge policy of repository %s in org %s set to %s by user %s",
        repository_id,
        org_id,
        payload.merge_policy or "inherit",
        user.get("user_id"),
    )
    return _merge_policy_view(rows[0], user.get("role"))


@app.post("/stop")
async def stop_agents(
    req: Optional[StopRequest] = None,
    org_id: str = Depends(get_org_id),
    role: str = Depends(require_write_role),
):
    """Stop/cancel a running agent run."""
    from agents import supabase as agents_supabase

    sb = agents_supabase
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")

    if req and req.run_id:
        # Cancel specific run
        run_id = req.run_id

        # Verify ownership before touching anything
        result = await asyncio.to_thread(
            lambda: sb.table("runs")
            .select("id, status, org_id")
            .eq("id", run_id)
            .eq("org_id", org_id)
            .limit(1)
            .maybe_single()
            .execute()
        )
        run = _row_or_none(result)

        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        if run["status"] not in ("queued", "running"):
            return {
                "status": "not_running",
                "run_id": run_id,
                "current_status": run["status"],
            }

        # Delegate to cancel_run_task -- it revokes the Celery task,
        # updates status to 'cancelled', AND releases the repo lock.
        # Calling the task function directly (not .delay()) runs it
        # synchronously here since it's just DB reads/writes plus a
        # control.revoke() call, not agent work.
        outcome = await asyncio.to_thread(cancel_run_task, run_id)
        if not outcome.get("success"):
            raise internal_error(
                500,
                "Cancellation failed",
                RuntimeError(outcome.get("error", "unknown error")),
            )

        return {"status": "cancelled", "run_id": run_id}

    else:
        # Cancel all running runs for org. This affects every member's runs
        # (and a /stop that races a /start arrives here without a run_id), so
        # it is reserved for org admins.
        if role not in ADMIN_ROLES:
            raise HTTPException(
                status_code=403,
                detail="Stopping all runs requires an org admin; pass run_id",
            )
        result = await asyncio.to_thread(
            lambda: sb.table("runs")
            .select("id")
            .eq("org_id", org_id)
            .in_("status", ["queued", "running"])
            .execute()
        )

        runs = result.data or []
        cancelled_count = 0

        for run in runs:
            outcome = await asyncio.to_thread(cancel_run_task, run["id"])
            if outcome.get("success"):
                cancelled_count += 1
            else:
                logger.warning(
                    f"Failed to cancel run {run['id']} during bulk stop: "
                    f"{outcome.get('error')}"
                )

        return {"status": "cancelled", "count": cancelled_count}


@app.post("/assist/inline")
async def inline_assist(
    req: InlineAssistRequest,
    user: dict = Depends(get_current_user),
):
    from agents import stream_inline_assist

    # Per-user and per-org quotas: each call spends the server's LLM budget.
    exceeded = await asyncio.to_thread(
        assist_limiter.check_assist, user["user_id"], user["org_id"]
    )
    if exceeded:
        raise HTTPException(
            status_code=429,
            detail=f"Inline assist quota exceeded ({exceeded.scope}); "
            f"retry in {exceeded.retry_after}s",
            headers={"Retry-After": str(exceeded.retry_after)},
        )

    try:
        generator = stream_inline_assist(
            prompt=req.prompt,
            selected_code=req.selected_code,
            prefix_code=req.prefix_code,
            suffix_code=req.suffix_code,
            file_path=req.file_path,
        )
        return StreamingResponse(generator, media_type="text/event-stream")
    except Exception as e:
        raise internal_error(500, "Inline assist processing failed", e)


# --- File operations ---


async def _github_client_for_repo(repo_name: str, org_id: str) -> Github:
    """PyGithub client acting as ``repo_name``'s GitHub App installation.

    The installation comes from the org-scoped repositories row, so a caller
    can only ever obtain a client for an installation linked to their own org.
    In the explicit single-tenant ``pat`` mode (see github_app) this is the
    GITHUB_TOKEN client instead, and no installation lookup is needed.
    """
    installation_id = None
    try:
        if get_auth_mode() == AUTH_MODE_APP:
            from agents import supabase as agents_supabase

            if not agents_supabase:
                raise HTTPException(status_code=503, detail="Database not configured")
            _, installation_id = await validate_repo_access(
                repo_name, org_id, "", agents_supabase
            )
        creds = credentials_for_installation(
            installation_id, purpose=f"{repo_name} file API", repository=repo_name
        )
        token = await asyncio.to_thread(creds.token)
    except GitHubAuthError as e:
        logger.error(f"No GitHub credentials for {repo_name}: {e}")
        raise HTTPException(
            status_code=503,
            detail="GitHub access is not configured for this repository",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise internal_error(502, "Could not obtain GitHub credentials", e)
    return Github(token)


def _get_github_repo(gh: Github, repo_name: str):
    try:
        return gh.get_repo(repo_name)
    except Exception as e:
        logger.info(f"get_repo({repo_name}) failed: {e}")
        raise HTTPException(status_code=404, detail="Repository not found")


def _write_local_mirror(repo_dir: Path, target_path: Path, content: str) -> None:
    """Best-effort write of an already-committed change into the local clone.

    The commit has succeeded by the time this runs, so a local failure is
    logged rather than turned into an error response for a change that landed.
    """
    # Defense in depth: callers pass paths from get_safe_repo_dir and
    # get_safe_target_path, but re-check containment right here so the write
    # is safe on its own (workspace root -> clone -> target).
    base = os.path.realpath(str(get_base_workspace_dir()))
    root = os.path.realpath(str(repo_dir))
    if not root.startswith(base + os.sep) or not os.path.isdir(root):
        return
    resolved = os.path.realpath(str(target_path))
    if not resolved.startswith(root + os.sep):
        logger.warning("Refusing local mirror write outside the repository clone")
        return
    target_path = Path(resolved)
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(content, encoding="utf-8")
    except Exception as e:
        logger.warning(f"Committed upstream but local mirror write failed: {e}")


# --- IDE commit target branch (#139) ---
#
# The IDE file routes used to commit straight to the repository's default
# branch, so every Save was an unreviewed commit to main while the UI implied
# the edit was only staged. They now commit to a working branch instead:
#
# - ``branch`` given: that branch, created from the default branch if missing.
# - ``branch`` omitted (what the dashboard sends today): the caller's IDE
#   working branch, ``automaintainer/ide-<hash of org and user>``. It is stable
#   per user, so consecutive saves accumulate on one branch, and the hash keeps
#   Supabase ids out of the repository's branch list.
#
# The default branch and any branch GitHub reports as protected are refused
# with 409 before anything is written. Getting the edits into the default
# branch is a pull request: POST /repo/{name}/propose-changes, or a PR opened
# from the working branch on GitHub.

IDE_BRANCH_PREFIX = "automaintainer/ide-"
# A conservative subset of git's ref-name rules (git check-ref-format).
_BRANCH_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*")
_BRANCH_NAME_MAX_LEN = 200
# Ref-qualified spellings git (and GitHub's ref resolution) would expand to an
# existing ref: "refs/heads/main", "heads/main", "tags/v1", "remotes/x/main".
# Accepting them would let "refs/heads/main" slip past the default-branch
# check, so a branch name must be a bare short name.
_REF_NAMESPACE_PREFIXES = ("refs/", "heads/", "tags/", "remotes/")


def _is_symbolic_ref_name(branch: str) -> bool:
    """HEAD, FETCH_HEAD, ORIG_HEAD, ...: symbolic refs, never a branch."""
    upper = branch.upper()
    return upper == "HEAD" or ("/" not in branch and upper.endswith("_HEAD"))


def _validate_branch_name(branch: str) -> None:
    if (
        len(branch) > _BRANCH_NAME_MAX_LEN
        or not _BRANCH_NAME_RE.fullmatch(branch)
        or ".." in branch
        or "/." in branch
        or branch.startswith((".", "-"))
        or branch.endswith((".", ".lock"))
        or branch.lower().startswith(_REF_NAMESPACE_PREFIXES)
        or _is_symbolic_ref_name(branch)
    ):
        raise HTTPException(status_code=400, detail="Invalid branch name")


def _same_branch(a: Optional[str], b: Optional[str]) -> bool:
    # Case-insensitive: git refs are case-sensitive, but on case-insensitive
    # filesystems "Main" and "main" collide, so treat them as the same branch.
    return isinstance(a, str) and isinstance(b, str) and a.casefold() == b.casefold()


def _ide_working_branch(org_id: str, user_id: str) -> str:
    digest = hashlib.sha256(f"{org_id}:{user_id}".encode("utf-8")).hexdigest()
    return f"{IDE_BRANCH_PREFIX}{digest[:12]}"


def _requested_ide_branch(requested: Optional[str], org_id: str, user_id: str) -> str:
    """The branch an IDE write targets, validated before any GitHub call."""
    if requested is None or not requested.strip():
        return _ide_working_branch(org_id, user_id)
    branch = requested.strip()
    _validate_branch_name(branch)
    return branch


def _sync_ide_branch(repo, branch: str, default_branch: str) -> None:
    """Merge the default branch into an existing IDE working branch.

    The IDE reads files from a clone synced to the default branch, so a file
    merged there after the working branch was created would be visible in
    the editor but missing on the branch it saves to (#139). The working
    branch is server-owned, so it is fast-forwarded or merged before each
    write. Only the working branch is written: ``base`` is the merge target.
    A conflict is reported rather than resolved, so no user edit is lost.

    Blocking (PyGithub); call from a worker thread.
    """
    try:
        # 204 (already up to date) returns None; 201 returns the merge commit.
        repo.merge(
            base=branch,
            head=default_branch,
            commit_message=f"Merge {default_branch} into {branch}",
        )
    except GithubException as e:
        if e.status == 409:
            raise HTTPException(
                status_code=409,
                detail="Your IDE working branch conflicts with the default "
                "branch; resolve the conflict on GitHub (or propose your "
                "changes as a pull request) before saving again",
            )
        raise internal_error(502, "Could not update the IDE working branch", e)
    except Exception as e:
        raise internal_error(502, "Could not update the IDE working branch", e)


def _ensure_writable_branch(repo, branch: str) -> None:
    """Refuse the default or a protected branch; create ``branch`` if missing.

    Blocking (PyGithub); call from a worker thread.
    """
    default_branch = repo.default_branch
    if not isinstance(default_branch, str) or _same_branch(branch, default_branch):
        # An unknown default branch fails closed.
        raise HTTPException(
            status_code=409,
            detail="The IDE does not commit to the default branch; "
            "omit 'branch' to use your IDE working branch, or propose changes "
            "as a pull request",
        )
    try:
        existing = repo.get_branch(branch)
    except UnknownObjectException:
        existing = None
    except Exception as e:
        raise internal_error(502, "Could not read the target branch", e)

    if existing is not None:
        # Anything but an explicit False fails closed.
        if getattr(existing, "protected", True) is not False:
            raise HTTPException(
                status_code=409, detail="The target branch is protected"
            )
        if branch.startswith(IDE_BRANCH_PREFIX):
            _sync_ide_branch(repo, branch, default_branch)
        return

    try:
        base = repo.get_git_ref(f"heads/{default_branch}")
        repo.create_git_ref(ref=f"refs/heads/{branch}", sha=base.object.sha)
    except GithubException as e:
        # 422 "Reference already exists": a concurrent save created it.
        if e.status != 422:
            raise internal_error(502, "Could not create the target branch", e)
    except Exception as e:
        raise internal_error(502, "Could not create the target branch", e)


@app.post("/repo/{repo_name:path}/file")
async def update_repo_file(
    repo_name: str,
    request: Request,
    payload: FileUpdateRequest,
    org_id: str = Depends(require_repo_write_access),
    user_id: str = Depends(get_user_id),
):
    if not re.fullmatch(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)?$", repo_name):
        raise HTTPException(status_code=400, detail="Invalid repository name")
    if (
        not re.fullmatch(r"^[a-zA-Z0-9_.\-/]+$", payload.file_path)
        or ".." in payload.file_path
    ):
        raise HTTPException(status_code=400, detail="Invalid file path")
    branch = _requested_ide_branch(payload.branch, org_id, user_id)

    # Validate the local mirror path BEFORE any GitHub call. Checking it only
    # after the commit meant a path the workspace refuses (.git/..., symlink
    # escapes) still got an upstream commit, followed by a 500.
    repo_dir = _repo_dir_for_request(request, repo_name)
    target_path = get_safe_target_path(repo_dir, payload.file_path)

    gh = await _github_client_for_repo(repo_name, org_id)
    message = (
        payload.commit_message or f"Update {payload.file_path} via AutoMaintainer IDE"
    )

    def _commit() -> None:
        # PyGithub and the mirror write are blocking I/O; run off the event loop.
        repo = _get_github_repo(gh, repo_name)
        _ensure_writable_branch(repo, branch)
        try:
            file = repo.get_contents(payload.file_path, ref=branch)
        except UnknownObjectException as e:
            logger.info(f"get_contents({payload.file_path}) on {repo_name} failed: {e}")
            raise HTTPException(status_code=404, detail="File not found in repo")
        except Exception as e:
            raise internal_error(502, "Could not read the file from GitHub", e)
        try:
            repo.update_file(
                file.path, message, payload.content, file.sha, branch=branch
            )
        except Exception as e:
            raise internal_error(500, "Failed to update file", e)

        # Mirror to the local clone so the IDE doesn't show stale reads. The
        # clone is reset to the remote on the next agent sync.
        _write_local_mirror(repo_dir, target_path, payload.content)

    await asyncio.to_thread(_commit)
    return {"status": "updated", "message": message, "branch": branch}


@app.post("/repo/{repo_name:path}/file/create")
async def create_repo_file(
    repo_name: str,
    request: Request,
    payload: FileCreateRequest,
    org_id: str = Depends(require_repo_write_access),
    user_id: str = Depends(get_user_id),
):
    if not re.fullmatch(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)?$", repo_name):
        raise HTTPException(status_code=400, detail="Invalid repository name")
    if (
        not re.fullmatch(r"^[a-zA-Z0-9_.\-/]+$", payload.file_path)
        or ".." in payload.file_path
    ):
        raise HTTPException(status_code=400, detail="Invalid file path")
    branch = _requested_ide_branch(payload.branch, org_id, user_id)

    actual_path = payload.file_path
    actual_content = payload.content
    if payload.is_dir:
        actual_path = f"{payload.file_path.rstrip('/')}/.gitkeep"
        actual_content = ""

    # Validate the local mirror path BEFORE committing upstream (see update).
    repo_dir = _repo_dir_for_request(request, repo_name)
    target_path = get_safe_target_path(repo_dir, actual_path)

    gh = await _github_client_for_repo(repo_name, org_id)

    message = (
        payload.commit_message or f"Create {payload.file_path} via AutoMaintainer IDE"
    )

    def _commit() -> None:
        # PyGithub and the mirror write are blocking I/O; run off the event loop.
        repo = _get_github_repo(gh, repo_name)
        _ensure_writable_branch(repo, branch)
        try:
            repo.create_file(actual_path, message, actual_content, branch=branch)
        except Exception as e:
            raise internal_error(500, "Failed to create file", e)

        _write_local_mirror(repo_dir, target_path, actual_content)

    await asyncio.to_thread(_commit)
    return {"status": "created", "message": message, "branch": branch}


@app.delete("/repo/{repo_name:path}/file")
async def delete_repo_file(
    repo_name: str,
    file_path: str,
    commit_message: Optional[str] = None,
    branch: Optional[str] = None,
    org_id: str = Depends(require_repo_write_access),
    user_id: str = Depends(get_user_id),
):
    if not re.fullmatch(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)?$", repo_name):
        raise HTTPException(status_code=400, detail="Invalid repository name")
    if not re.fullmatch(r"^[a-zA-Z0-9_.\-/]+$", file_path) or ".." in file_path:
        raise HTTPException(status_code=400, detail="Invalid file path")
    target_branch = _requested_ide_branch(branch, org_id, user_id)

    gh = await _github_client_for_repo(repo_name, org_id)
    message = commit_message or f"Delete {file_path} via AutoMaintainer IDE"

    def _delete() -> None:
        # PyGithub calls are blocking HTTP round trips; run off the event loop.
        repo = _get_github_repo(gh, repo_name)
        _ensure_writable_branch(repo, target_branch)
        try:
            file = repo.get_contents(file_path, ref=target_branch)
        except UnknownObjectException as e:
            logger.info(f"get_contents({file_path}) on {repo_name} failed: {e}")
            raise HTTPException(status_code=404, detail="File not found in repo")
        except Exception as e:
            raise internal_error(502, "Could not read the file from GitHub", e)

        if isinstance(file, list):
            raise HTTPException(
                status_code=400,
                detail="Directories cannot be deleted directly via this endpoint.",
            )

        try:
            repo.delete_file(file.path, message, file.sha, branch=target_branch)
        except Exception as e:
            raise internal_error(500, "Failed to delete file", e)

    await asyncio.to_thread(_delete)
    return {"status": "deleted", "message": message, "branch": target_branch}


# --- Propose Changes Endpoint ---
# Creates a PR with staged changes from WebIDE


class ProposedChange(BaseModel):
    path: str
    content: str = ""
    status: str = "modified"


class ProposeChangesRequest(BaseModel):
    title: str
    description: Optional[str] = None
    changes: List[ProposedChange]


@app.post("/repo/{repo_name:path}/propose-changes")
async def propose_changes(
    repo_name: str,
    payload: ProposeChangesRequest,
    org_id: str = Depends(get_org_id),
    user_id: str = Depends(get_user_id),
    _role: str = Depends(require_write_role),
):
    """
    Create a new branch, commit changes, and open a Pull Request.
    Used by WebIDE for the preview→PR flow.
    """
    if not re.fullmatch(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)?$", repo_name):
        raise HTTPException(status_code=400, detail="Invalid repository name")

    # Authenticated is not authorized: confirm this repo belongs to the
    # caller's org before opening a PR against it.
    from agents import supabase as agents_supabase

    if not agents_supabase:
        raise HTTPException(status_code=503, detail="Database not configured")
    await validate_repo_access(repo_name, org_id, user_id, agents_supabase)

    gh = await _github_client_for_repo(repo_name, org_id)

    def _open_pull_request() -> dict:
        # Every PyGithub call below is a blocking HTTP round trip (one per
        # changed file plus branch/PR calls), so the whole sequence runs in a
        # worker thread instead of stalling the event loop.
        repo = _get_github_repo(gh, repo_name)

        # Get default branch
        default_branch = repo.default_branch

        # Create new branch name
        safe_title = "".join(
            c if c.isalnum() or c in "-_" else "-" for c in payload.title.lower()
        )
        safe_title = safe_title[:50].strip("-")
        branch_name = f"automaintainer/{safe_title}-{uuid.uuid4().hex[:8]}"

        try:
            # Get base commit SHA
            base_ref = repo.get_git_ref(f"heads/{default_branch}")
            base_sha = base_ref.object.sha

            # Create new branch
            repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=base_sha)

            # Commit each change
            for change in payload.changes:
                file_path = change.path
                content = change.content
                status = change.status

                if (
                    not re.fullmatch(r"^[a-zA-Z0-9_.\-/]+$", file_path)
                    or ".." in file_path
                ):
                    continue

                if status == "deleted":
                    # Delete file
                    try:
                        file = repo.get_contents(file_path, ref=branch_name)
                        repo.delete_file(
                            file.path,
                            f"Delete {file_path} via AutoMaintainer IDE",
                            file.sha,
                            branch=branch_name,
                        )
                    except Exception as e:
                        logger.warning(f"Failed to delete {file_path}: {e}")
                else:
                    # Create or update file
                    message = f"{'Create' if status == 'created' else 'Update'} {file_path} via AutoMaintainer IDE"
                    try:
                        file = repo.get_contents(file_path, ref=branch_name)
                        repo.update_file(
                            file.path, message, content, file.sha, branch=branch_name
                        )
                    except Exception:
                        # File doesn't exist, create it
                        repo.create_file(
                            file_path, message, content, branch=branch_name
                        )

            # Create Pull Request
            pr = repo.create_pull(
                title=payload.title,
                body=payload.description
                or "Changes proposed via AutoMaintainer WebIDE",
                head=branch_name,
                base=default_branch,
            )

            return {
                "branch_name": branch_name,
                "pr_number": pr.number,
                "pr_url": pr.html_url,
                "base_branch": default_branch,
            }

        except Exception as e:
            # Try to clean up branch on failure
            try:
                ref = repo.get_git_ref(f"heads/{branch_name}")
                ref.delete()
            except Exception:
                pass
            raise internal_error(500, "Failed to propose changes", e)

    return await asyncio.to_thread(_open_pull_request)


# --- GitHub App installation claim ---
#
# The only way an installation becomes linked to an org: clients can no longer
# insert github_installations rows (the RLS insert policy is service-role
# only), and this endpoint proves ownership against GitHub before writing.


class InstallationClaimRequest(BaseModel):
    installation_id: int


@app.post("/github/installations/claim")
async def claim_github_installation(
    payload: InstallationClaimRequest,
    user: dict = Depends(get_current_user),
    x_github_user_token: Annotated[str | None, Header()] = None,
):
    """Link a GitHub App installation to the caller's org.

    Requires an org owner/admin, plus the caller's GitHub user token in
    ``X-GitHub-User-Token`` (the dashboard sends the Supabase GitHub
    provider_token). GitHub must confirm the installation is accessible to
    that user and that its account is the user or an org they administer
    (github_app.verify_installation_claim). An installation already linked to
    another org is never moved (409).
    """
    if user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only organization owners and admins can link GitHub installations",
        )
    if payload.installation_id <= 0:
        raise HTTPException(status_code=400, detail="Invalid installation id")
    user_token = (x_github_user_token or "").strip()
    if not user_token:
        raise HTTPException(
            status_code=400, detail="Missing X-GitHub-User-Token header"
        )

    from agents import supabase as sb

    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")
    org_id = user["org_id"]

    try:
        record = await verify_installation_claim(user_token, payload.installation_id)
    except InstallationClaimError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        raise internal_error(502, "Could not verify the installation with GitHub", e)

    await _link_installation(sb, org_id, record)
    return {
        "status": "claimed",
        "installation_id": record["id"],
        "account_login": record["account_login"],
        "account_type": record["account_type"],
    }


async def _link_installation(sb, org_id: str, record: dict) -> None:
    """Write a verified installation ``record`` for ``org_id``.

    Shared by the claim endpoint and the install callback. An installation
    already linked to another org is never moved (409).
    """
    try:
        existing = await asyncio.to_thread(
            lambda: sb.table("github_installations")
            .select("id, org_id")
            .eq("id", record["id"])
            .limit(1)
            .execute()
        )
    except Exception as e:
        raise internal_error(500, "Failed to look up installation", e)
    rows = existing.data if isinstance(existing.data, list) else []
    if rows and str(rows[0].get("org_id")) != str(org_id):
        logger.warning(
            f"Org {org_id} tried to claim installation {record['id']}, "
            f"which is linked to org {rows[0].get('org_id')}"
        )
        raise HTTPException(
            status_code=409,
            detail="This installation is already linked to another organization",
        )

    row = {**record, "org_id": org_id}
    try:
        if rows:
            # Same org re-claiming: refresh GitHub-derived fields only.
            await asyncio.to_thread(
                lambda: sb.table("github_installations")
                .update(row)
                .eq("id", record["id"])
                .eq("org_id", org_id)
                .execute()
            )
        else:
            # A plain insert, not an upsert: if another org links the same id
            # concurrently, the primary key rejects this write instead of
            # moving the row.
            await asyncio.to_thread(
                lambda: sb.table("github_installations").insert(row).execute()
            )
    except Exception as e:
        raise internal_error(409, "Failed to link installation", e)


# --- GitHub App install flow -------------------------------------------------
#
# 1. POST /github/app/install-url (org owner/admin, with the GitHub user token
#    in X-GitHub-User-Token) returns the App's install URL carrying a signed,
#    expiring ``state`` bound to (user, org, GitHub user id); see
#    github_app.create_setup_state.
# 2. The App is registered with "Request user authorization (OAuth) during
#    installation", so after the install GitHub sends the browser to
#    GET /github/app/setup with installation_id, setup_action, state and an
#    OAuth code.
# 3. The callback verifies the state, re-checks that the user is still an
#    owner/admin of the org, exchanges the code for the installer's
#    user-to-server token, and runs the same verification and write as
#    POST /github/installations/claim (verify_installation_claim, which here
#    also requires the token to belong to the GitHub user named in the state).
#    The installation_id in the URL is only ever a lookup key into what that
#    token can access, so a spoofed id is rejected.
#
# The browser is then redirected to the dashboard with only a fixed status
# word (and the verified installation id on success). The redirect target is
# server configuration only (DASHBOARD_URL, or the dashboard this API serves
# at "/"); no request parameter or header influences it, so the route cannot
# be used as an open redirect.

from fastapi.responses import RedirectResponse  # noqa: E402
from urllib.parse import urlencode  # noqa: E402

GITHUB_SETUP_ACTIONS = frozenset({"install", "update", "request"})
# GitHub ids are positive 64-bit integers; ASCII digits only (str.isdigit
# would also accept other Unicode digits).
_INSTALLATION_ID_RE = re.compile(r"[0-9]{1,19}")
# GitHub OAuth codes are short opaque tokens; anything else is not forwarded.
_OAUTH_CODE_RE = re.compile(r"[A-Za-z0-9_.-]{1,256}")


@app.post("/github/app/install-url")
async def github_app_install_url(
    user: dict = Depends(get_current_user),
    x_github_user_token: Annotated[str | None, Header()] = None,
):
    """Return the GitHub App install URL with a signed ``state`` for the
    caller (org owner/admin). The state expires after
    SETUP_STATE_TTL_SECONDS seconds."""
    if user.get("role") not in ADMIN_ROLES:
        raise HTTPException(
            status_code=403,
            detail="Only organization owners and admins can install the GitHub App",
        )
    if not install_flow_configured():
        raise HTTPException(
            status_code=503,
            detail="GitHub App install flow is not configured "
            "(GITHUB_APP_SLUG, GITHUB_APP_CLIENT_ID, GITHUB_APP_CLIENT_SECRET)",
        )
    user_token = (x_github_user_token or "").strip()
    if not user_token:
        raise HTTPException(
            status_code=400, detail="Missing X-GitHub-User-Token header"
        )
    try:
        github_user_id = await github_user_id_for_token(user_token)
    except InstallationClaimError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    except Exception as e:
        raise internal_error(502, "Could not identify the GitHub user", e)

    state = create_setup_state(user["user_id"], user["org_id"], github_user_id)
    return {
        "install_url": build_install_url(state),
        "expires_in": SETUP_STATE_TTL_SECONDS,
    }


def _dashboard_base_url() -> str:
    """DASHBOARD_URL as a bare http(s) origin, or "" for the API's own "/"."""
    configured = (os.getenv("DASHBOARD_URL") or "").strip()
    if not configured:
        return ""
    parsed = urlsplit(configured)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "@" in parsed.netloc
    ):
        logger.error(
            "DASHBOARD_URL must be an http(s) origin without a path; "
            "redirecting GitHub App setup callbacks to / instead"
        )
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _parse_installation_id(raw: Optional[str]) -> Optional[int]:
    if not raw or not _INSTALLATION_ID_RE.fullmatch(raw):
        return None
    iid = int(raw)
    return iid if 0 < iid < 2**63 else None


async def _org_admin_still(sb, user_id: str, org_id: str) -> bool:
    """The state's user is still an accepted owner/admin of the state's org."""
    result = await asyncio.to_thread(
        lambda: sb.table("organization_members")
        .select("role, joined_at")
        .eq("user_id", user_id)
        .eq("org_id", org_id)
        .limit(1)
        .execute()
    )
    rows = result.data if isinstance(getattr(result, "data", None), list) else []
    return bool(
        rows and rows[0].get("joined_at") and rows[0].get("role") in ADMIN_ROLES
    )


_CLAIM_ERROR_OUTCOMES = {401: "unauthorized", 403: "forbidden"}


async def _complete_app_setup(
    installation_id: Optional[str],
    setup_action: Optional[str],
    state: Optional[str],
    code: Optional[str],
) -> dict:
    """Run the verified claim for a setup callback; return redirect params."""
    if setup_action not in GITHUB_SETUP_ACTIONS:
        return {"github_setup": "invalid"}
    if setup_action == "request" and not installation_id:
        # An org member asked an owner to install the App; nothing to link.
        return {"github_setup": "requested"}
    iid = _parse_installation_id(installation_id)
    if iid is None:
        return {"github_setup": "invalid"}
    if not state or not code:
        # Not started from POST /github/app/install-url (or OAuth during
        # installation is off): nothing proves who is installing, so nothing
        # is linked. The dashboard can still link it with the verified
        # POST /github/installations/claim.
        return {"github_setup": "unverified"}
    if not _OAUTH_CODE_RE.fullmatch(code):
        return {"github_setup": "invalid"}
    if not install_flow_configured():
        return {"github_setup": "unavailable"}
    try:
        claim = verify_setup_state(state)
    except SetupStateError as e:
        logger.warning(f"GitHub App setup callback with a bad state: {e}")
        expired = isinstance(e, SetupStateExpired)
        return {"github_setup": "expired" if expired else "invalid"}

    from agents import supabase as sb

    if not sb:
        return {"github_setup": "unavailable"}
    user_id, org_id = claim["u"], claim["o"]
    try:
        if not await _org_admin_still(sb, user_id, org_id):
            return {"github_setup": "forbidden"}
        user_token = await exchange_oauth_code(code)
        record = await verify_installation_claim(
            user_token, iid, expected_github_user_id=claim["g"]
        )
        await _link_installation(sb, org_id, record)
    except InstallationClaimError as e:
        logger.warning(f"GitHub App setup for org {org_id} rejected: {e.detail}")
        return {"github_setup": _CLAIM_ERROR_OUTCOMES.get(e.status_code, "error")}
    except HTTPException as e:
        return {"github_setup": "conflict" if e.status_code == 409 else "error"}
    except Exception as e:
        logger.error(f"GitHub App setup for org {org_id} failed: {e}")
        return {"github_setup": "error"}
    return {"github_setup": "linked", "github_installation_id": str(record["id"])}


@app.get("/github/app/setup", include_in_schema=False)
async def github_app_setup_callback(
    installation_id: Optional[str] = None,
    setup_action: Optional[str] = None,
    state: Optional[str] = None,
    code: Optional[str] = None,
):
    """GitHub App setup URL: verify the signed state and the installer's
    OAuth code, link the installation, then redirect to the dashboard."""
    params = await _complete_app_setup(installation_id, setup_action, state, code)
    location = f"{_dashboard_base_url()}/?{urlencode(params)}"
    response = RedirectResponse(location, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    # The URL carried a single-use OAuth code; keep it out of Referer headers.
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


# --- Terminal WebSocket ---
#
# Policy: disabled unless ENABLE_TERMINAL=true (in every environment), and even
# then only for Supabase user ids listed in TERMINAL_ADMIN_USER_IDS. The shell
# gets a minimal allowlisted environment, and a requested repo is authorized
# against the caller's org before its clone is used as the working directory.
#
# LIMITATION: the env scrub is defense in depth, not isolation. The shell runs
# as the same OS user as the API server, so it can still read
# /proc/<server pid>/environ, backend/.env and every tenant's clone. Do not set
# ENABLE_TERMINAL on shared or hosted deployments until the shell runs as a
# separate unprivileged user or in a sandbox.

# Only these variables reach the interactive shell. Everything else in the
# server's environment (SUPABASE_SERVICE_KEY, GITHUB_TOKEN, GITHUB_APP_*,
# GROQ_API_KEY*, database/Redis URLs, ...) is withheld. An allowlist rather
# than a denylist, so a newly added secret is withheld by default.
_TERMINAL_ENV_ALLOWLIST = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TZ"}
)
# Windows needs these for cmd.exe and most programs to start at all. None of
# them carries a secret.
_TERMINAL_ENV_ALLOWLIST_WIN32 = frozenset(
    {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE"}
)

# "owner/repo", or a github.com URL to it (what the dashboard sends).
# Characters GitHub allows in owner and repository names. Validated with a set
# check rather than a regex so parsing is trivially linear in the input length.
_TERMINAL_REPO_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-"
)
_TERMINAL_REPO_MAX_LEN = 256


def _terminal_enabled() -> bool:
    """The terminal is opt-in in every environment.

    Keying the default on ENVIRONMENT=production left it enabled in any
    deployment that never set ENVIRONMENT -- including the shipped Dockerfile.
    """
    return os.getenv("ENABLE_TERMINAL", "").strip().lower() == "true"


def _terminal_admin_ids() -> set[str]:
    raw = os.getenv("TERMINAL_ADMIN_USER_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _terminal_user_allowed(user_id: str) -> bool:
    """Deployment-level allowlist; empty means nobody.

    Org roles cannot gate this: organizations are self-serve, so every signup
    is the owner of its own org.
    """
    return bool(user_id) and str(user_id) in _terminal_admin_ids()


def _terminal_child_env(windows: bool = False) -> dict:
    allowed = _TERMINAL_ENV_ALLOWLIST | (
        _TERMINAL_ENV_ALLOWLIST_WIN32 if windows else frozenset()
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k.upper() in allowed or k.startswith("LC_")
    }
    if not windows:
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["TERM"] = "xterm-256color"
    return env


def _parse_terminal_repo(repo_url: str) -> Optional[str]:
    """Accept "owner/repo" or a github.com URL (optional .git / trailing slash)."""
    if len(repo_url) > _TERMINAL_REPO_MAX_LEN:
        return None
    slug = repo_url
    for prefix in (
        "https://www.github.com/",
        "http://www.github.com/",
        "https://github.com/",
        "http://github.com/",
    ):
        if slug.startswith(prefix):
            slug = slug[len(prefix) :]
            break
    if slug.endswith("/"):
        slug = slug[:-1]
    if slug.endswith(".git"):
        slug = slug[: -len(".git")]
    parts = slug.split("/")
    if len(parts) != 2:
        return None
    owner, repo = parts
    for part in (owner, repo):
        if not part or part in (".", "..") or not set(part) <= _TERMINAL_REPO_CHARS:
            return None
    return f"{owner}/{repo}"


def _use_winpty() -> bool:
    return sys.platform == "win32"


async def _authorize_terminal_cwd(
    repo_url: str, user: dict
) -> tuple[bool, Optional[str]]:
    """Return (allowed, cwd) for the requested repo.

    An unparseable repo, or one outside the caller's org, refuses the
    connection rather than silently falling back to another directory.
    """
    if not repo_url:
        return True, None

    repo_name = _parse_terminal_repo(repo_url)
    if not repo_name:
        return False, None

    from agents import supabase as agents_supabase

    if not agents_supabase:
        return False, None
    try:
        repository_id, _ = await validate_repo_access(
            repo_name, user.get("org_id"), user.get("user_id"), agents_supabase
        )
    except Exception:
        # HTTPException(404) for an unknown repo, or a database error.
        return False, None

    try:
        # Keyed by the repository id like the agent's clone (runs.repository_id).
        repo_dir = get_safe_repo_dir(repo_name, repository_id)
    except HTTPException:
        return False, None
    real_dir = os.path.realpath(str(repo_dir))
    real_base = os.path.realpath(str(get_base_workspace_dir()))
    if real_dir.startswith(real_base + os.path.sep) and os.path.isdir(real_dir):
        return True, real_dir
    # Authorized but not cloned yet: open the shell without a repo cwd.
    return True, None


# Application close codes (RFC 6455 reserves 4000-4999 for applications),
# mirroring HTTP 401 and 403.
WS_CLOSE_UNAUTHENTICATED = 4401
WS_CLOSE_FORBIDDEN = 4403


async def _refuse_websocket(websocket: WebSocket, code: int) -> None:
    """Refuse a handshake that already failed authentication or authorization.

    Every check runs before this is called, so nothing has been spawned or
    read. Closing before accept() makes the ASGI server answer with a bare
    HTTP 403, which a browser reports as close code 1006 for every reason; to
    let a client tell "sign in again" (4401) from "not allowed" (4403), the
    handshake is completed and closed at once with the code. The "bearer"
    subprotocol is echoed only when offered (browsers fail a handshake that
    selects a protocol they did not offer); the token is never echoed.
    """
    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="bearer" if "bearer" in offered else None)
    await websocket.close(code=code)


@app.websocket("/api/terminal/ws")
async def terminal_ws(websocket: WebSocket, repo_url: str = ""):
    """Interactive shell (/bin/bash, or COMSPEC on Windows) as the API's OS user.

    Checks, all before the handshake is accepted: exact Origin match, a
    Supabase token of an accepted org member (4401 otherwise),
    ENABLE_TERMINAL=true, the user id on TERMINAL_ADMIN_USER_IDS, and repo_url
    belonging to the caller's org (4403 otherwise; another org's repository
    and a missing one are indistinguishable).
    """
    # Fail closed. The previous guard was `if origin and not (...)`, which
    # skipped the check entirely when Origin was absent -- every non-browser
    # client (websocat, curl, python-websockets) omits it by default. It also
    # matched by substring, so "huggingface.co" accepted
    # https://huggingface.co.attacker.net. Require an exact match against the
    # configured allowlist; set ALLOWED_ORIGINS to permit another origin. The
    # CORS preview-deployment regex is deliberately NOT honoured here -- an
    # interactive shell should not be reachable from arbitrary preview hosts.
    #
    # Checked first and refused without completing the handshake, so a page on
    # a foreign origin learns nothing (not even whether the terminal is on).
    origin = websocket.headers.get("origin")
    if not origin or origin not in ALLOWED_ORIGINS:
        await websocket.close(code=WS_CLOSE_FORBIDDEN)
        return

    # The token is checked before ENABLE_TERMINAL so an unauthenticated
    # caller gets 4401 either way and cannot tell whether the terminal is on.
    user = await authenticate_websocket(websocket)
    if not user:
        await _refuse_websocket(websocket, WS_CLOSE_UNAUTHENTICATED)
        return
    if not _terminal_enabled():
        await _refuse_websocket(websocket, WS_CLOSE_FORBIDDEN)
        return
    if not _terminal_user_allowed(user.get("user_id")):
        await _refuse_websocket(websocket, WS_CLOSE_FORBIDDEN)
        return

    allowed, cwd = await _authorize_terminal_cwd(repo_url, user)
    if not allowed:
        await _refuse_websocket(websocket, WS_CLOSE_FORBIDDEN)
        return

    await websocket.accept(subprotocol="bearer")

    if _use_winpty():
        # The module is `winpty`, from the `pywinpty` package.
        import winpty

        cols, rows = 80, 24
        pty = winpty.PTY(cols, rows)
        child_env = _terminal_child_env(windows=True)
        shell = child_env.get("COMSPEC") or "cmd.exe"
        # pywinpty takes the environment as "K=V\0K=V\0" and returns a bool;
        # the child's pid is exposed separately as pty.pid.
        env_block = "".join(f"{k}={v}\0" for k, v in child_env.items())
        pty.spawn(shell, cwd=cwd, env=env_block)
        pid = pty.pid

        async def read_from_pty():
            while True:
                try:
                    data = await asyncio.to_thread(pty.read)
                    if data:
                        await websocket.send_text(data)
                    else:
                        await asyncio.sleep(0.01)
                except Exception:
                    break

        read_task = asyncio.create_task(read_from_pty())

        try:
            while True:
                message = await websocket.receive_text()
                if message.startswith('{"type":"resize"'):
                    msg_data = json.loads(message)
                    pty.set_size(msg_data["cols"], msg_data["rows"])
                else:
                    await asyncio.to_thread(pty.write, message)
        except Exception:
            pass
        finally:
            read_task.cancel()
            if pid:
                try:
                    import signal

                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            try:
                del pty
            except Exception:
                pass
    else:
        import pty
        import fcntl
        import termios
        import struct
        import signal

        # Computed before fork(): the child should only exec.
        child_env = _terminal_child_env()
        pid, fd = pty.fork()
        if pid == 0:
            if cwd:
                os.chdir(cwd)
            os.execve("/bin/bash", ["/bin/bash"], child_env)
        else:

            async def read_from_pty():
                while True:
                    try:
                        data = await asyncio.to_thread(os.read, fd, 1024)
                        if data:
                            await websocket.send_text(
                                data.decode("utf-8", errors="replace")
                            )
                        else:
                            break
                    except Exception:
                        break

            read_task = asyncio.create_task(read_from_pty())

            try:
                while True:
                    message = await websocket.receive_text()
                    if message.startswith('{"type":"resize"'):
                        msg_data = json.loads(message)
                        winsize = struct.pack(
                            "HHHH", msg_data["rows"], msg_data["cols"], 0, 0
                        )
                        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
                    else:
                        await asyncio.to_thread(os.write, fd, message.encode("utf-8"))
            except Exception:
                pass
            finally:
                read_task.cancel()
                try:
                    os.close(fd)
                except Exception:
                    pass
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except Exception:
                    pass


# --- Repository browsing endpoints (unchanged) ---


@app.get("/repo/{repo_name:path}/tree")
def get_repo_tree(
    repo_name: str,
    request: Request,
    org_id: str = Depends(require_repo_access),
):
    repo_dir = _repo_dir_for_request(request, repo_name)

    if not os.path.exists(repo_dir):
        raise HTTPException(
            status_code=404,
            detail="Repository not cloned yet. Start an agent loop first.",
        )

    ignored_dirs = {
        ".git",
        "node_modules",
        "__pycache__",
        "venv",
        "env",
        "build",
        "dist",
    }

    def build_tree(path):
        tree = []
        try:
            for item in os.listdir(path):
                if item in ignored_dirs:
                    continue
                item_path = os.path.join(path, item)

                # Prevent symlink loops
                if os.path.islink(item_path):
                    continue

                is_dir = os.path.isdir(item_path)
                node = {
                    "name": item,
                    "path": Path(item_path).relative_to(repo_dir).as_posix(),
                    "type": "directory" if is_dir else "file",
                }
                if is_dir:
                    node["children"] = build_tree(item_path)
                tree.append(node)
        except (OSError, PermissionError) as e:
            logger.warning(f"Error accessing path {path}: {e}")
            raise HTTPException(status_code=500, detail="Error accessing file system")

        # Sort directories first, then files
        tree.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))
        return tree

    return {"name": repo_name, "type": "directory", "children": build_tree(repo_dir)}


@app.get("/repo/{repo_name:path}/search")
def search_repo(
    repo_name: str,
    request: Request,
    q: str,
    org_id: str = Depends(require_repo_access),
):
    repo_dir = _repo_dir_for_request(request, repo_name)

    if not repo_dir.exists():
        raise HTTPException(status_code=404, detail="Repo not found locally")

    ignored_dirs = {
        ".git",
        "node_modules",
        "__pycache__",
        "venv",
        "env",
        "build",
        "dist",
        ".next",
    }
    results = []

    try:
        for root, dirs, files in os.walk(repo_dir):
            dirs[:] = [
                d
                for d in dirs
                if d not in ignored_dirs and not os.path.islink(os.path.join(root, d))
            ]
            for file in files:
                file_path = os.path.join(root, file)
                if os.path.islink(file_path):
                    continue
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        for i, line in enumerate(f):
                            if q.lower() in line.lower():
                                rel_path = (
                                    Path(file_path).relative_to(repo_dir).as_posix()
                                )
                                results.append(
                                    {
                                        "file": rel_path,
                                        "line_number": i + 1,
                                        "snippet": line.strip()[:200],
                                    }
                                )
                except UnicodeDecodeError:
                    pass
                except Exception:
                    pass
    except Exception as e:
        raise internal_error(500, "Search failed", e)

    return {"query": q, "results": results[:100]}


@app.get("/repo/{repo_name:path}/file")
def get_repo_file(
    repo_name: str,
    request: Request,
    file_path: str,
    org_id: str = Depends(require_repo_access),
):
    repo_dir = _repo_dir_for_request(request, repo_name)
    target_path = get_safe_target_path(repo_dir, file_path)

    import os, stat

    try:
        fd = os.open(target_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise HTTPException(status_code=404, detail="File not found")

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise HTTPException(status_code=404, detail="File not found")

        # Pass closefd=False so the finally block retains exclusive ownership of closing fd
        with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as f:
            content = f.read()
        return {"content": content}
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="Cannot read binary file")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to read file {target_path}: {e}")
        raise HTTPException(
            status_code=500, detail="An internal error occurred while reading the file"
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# --- Admin Endpoints ---
# Admin role is checked in-handler below. Note the backend uses the Supabase
# SERVICE ROLE key, which bypasses Row Level Security, so every query here
# must scope itself to the caller's org explicitly. RLS is not a backstop.


@app.get("/admin/metrics")
async def get_admin_metrics(
    org_id: str = Depends(get_org_id), user_id: str = Depends(get_user_id)
):
    """Get metrics for the caller's organization admin dashboard."""
    from agents import supabase as agents_supabase

    sb = agents_supabase
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Check if user is admin
    try:
        is_admin = await asyncio.to_thread(
            lambda: sb.rpc("user_is_org_admin", {"org_uuid": org_id}).execute()
        )
        if not is_admin.data:
            raise HTTPException(status_code=403, detail="Admin privileges required")
    except Exception:
        # Fallback: check via organization_members
        result = await asyncio.to_thread(
            lambda: sb.table("organization_members")
            .select("role")
            .eq("org_id", org_id)
            .eq("user_id", user_id)
            .limit(1)
            .maybe_single()
            .execute()
        )
        member = _row_or_none(result)
        if not member or member.get("role") not in ADMIN_ROLES:
            raise HTTPException(status_code=403, detail="Admin privileges required")

    # Fetch metrics from database views
    try:
        # Org health overview
        org_health = await asyncio.to_thread(
            lambda: sb.table("org_health").select("*").eq("org_id", org_id).execute()
        )

        # Recent runs
        recent_runs = await asyncio.to_thread(
            lambda: sb.table("recent_runs_detailed")
            .select("*")
            .eq("org_id", org_id)
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )

        # Usage summary (last 30 days)
        usage_summary = await asyncio.to_thread(
            lambda: sb.table("monthly_usage_summary")
            .select("*")
            .eq("org_id", org_id)
            .gte(
                "month",
                (datetime.utcnow().replace(day=1) - timedelta(days=30)).isoformat(),
            )
            .execute()
        )

        # Aggregate metrics
        orgs = org_health.data or []
        runs = recent_runs.data or []
        usage = usage_summary.data or []

        # Calculate totals
        total_orgs = len(orgs)
        active_orgs = sum(1 for o in orgs if o.get("active_runs", 0) > 0)
        total_runs = len(runs)
        running_runs = sum(1 for r in runs if r.get("status") == "running")
        completed_runs = sum(1 for r in runs if r.get("status") == "completed")
        failed_runs = sum(1 for r in runs if r.get("status") == "failed")
        runs_24h = sum(
            1
            for r in runs
            if r.get("created_at", "")
            > (datetime.utcnow() - timedelta(days=1)).isoformat()
        )

        total_tokens = sum(
            u.get("total_quantity", 0)
            for u in usage
            if u.get("event_type") == "llm_tokens_consumed"
        )
        total_cost = sum(u.get("total_estimated_cost_cents", 0) for u in usage)

        # Usage by event type
        by_event = {}
        for u in usage:
            et = u.get("event_type", "unknown")
            if et not in by_event:
                by_event[et] = {"count": 0, "costCents": 0}
            by_event[et]["count"] += u.get("total_quantity", 0)
            by_event[et]["costCents"] += u.get("total_estimated_cost_cents", 0)

        # Daily usage for last 30 days
        daily_usage = []
        for i in range(30):
            day = datetime.utcnow() - timedelta(days=i)
            day_str = day.strftime("%Y-%m-%d")
            day_data = [u for u in usage if u.get("month", "").startswith(day_str[:7])]
            day_tokens = sum(
                u.get("total_quantity", 0)
                for u in day_data
                if u.get("event_type") == "llm_tokens_consumed"
            )
            day_cost = sum(u.get("total_estimated_cost_cents", 0) for u in day_data)
            daily_usage.append(
                {"date": day_str, "tokens": day_tokens, "costCents": day_cost}
            )
        daily_usage.reverse()

        # Plan distribution
        plan_dist = {}
        for o in orgs:
            plan = o.get("plan", "free")
            plan_dist[plan] = plan_dist.get(plan, 0) + 1

        return {
            "orgs": {
                "total": total_orgs,
                "active": active_orgs,
                "byPlan": plan_dist,
            },
            "organizations": orgs,
            "runs": {
                "total": total_runs,
                "running": running_runs,
                "completed": completed_runs,
                "failed": failed_runs,
                "last24h": runs_24h,
            },
            "usage": {
                "totalTokens": total_tokens,
                "totalCostCents": total_cost,
                "byEventType": by_event,
                "last30Days": daily_usage,
            },
            "repos": {
                "total": sum(o.get("repo_count", 0) for o in orgs),
                "private": 0,  # Would need separate query
                "public": 0,
            },
            "system": {
                "supabaseHealthy": True,
                "activeConnections": None,
                "queueDepth": None,
            },
        }
    except Exception as e:
        raise internal_error(500, "Failed to fetch metrics", e)


@app.get("/admin/runs")
async def get_admin_runs(
    org_id: str = Depends(get_org_id),
    user_id: str = Depends(get_user_id),
    status: Optional[str] = None,
    limit: int = Query(100, ge=1, le=100),
    offset: int = Query(0, ge=0, le=100_000),
):
    """Get all runs across organization for admin."""
    from agents import supabase as agents_supabase

    sb = agents_supabase
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")

    # Check admin
    result = await asyncio.to_thread(
        lambda: sb.table("organization_members")
        .select("role")
        .eq("org_id", org_id)
        .eq("user_id", user_id)
        .limit(1)
        .maybe_single()
        .execute()
    )
    member = _row_or_none(result)
    if not member or member.get("role") not in ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Admin privileges required")

    query = (
        sb.table("runs").select("*").eq("org_id", org_id).order("created_at", desc=True)
    )

    if status:
        query = query.eq("status", status)

    result = await asyncio.to_thread(
        lambda: query.range(offset, offset + limit - 1).execute()
    )

    return {"runs": result.data or []}


# ---------------------------------------------------------------------------
# Static frontend -- MUST be the last thing registered on the app.
#
# Starlette matches routes in registration order and Mount("/") matches every
# path. Mounting before the API routes are declared silently shadows them:
# /admin/metrics returned the SPA's 404 page instead of JSON whenever
# dashboard/out existed, which is always true inside the Docker image.
# Keep this block at the bottom of the module.
# ---------------------------------------------------------------------------
# Serve the static Next.js frontend if the out directory exists
if os.path.exists("../dashboard/out"):
    app.mount(
        "/", StaticFiles(directory="../dashboard/out", html=True), name="dashboard"
    )
elif os.path.exists("dashboard/out"):  # In docker container
    app.mount("/", StaticFiles(directory="dashboard/out", html=True), name="dashboard")
