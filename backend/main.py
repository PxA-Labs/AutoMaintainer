from fastapi import (
    FastAPI,
    WebSocket,
    WebSocketDisconnect,
    BackgroundTasks,
    HTTPException,
    Depends,
    Header,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from github import Github
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from typing import Optional, Dict, List, Annotated
from agents import run_agent_loop, broadcast_log
from workspace import get_safe_repo_dir, get_safe_target_path, get_base_workspace_dir
import asyncio
import json
import uuid
import httpx
import os
import sys
import platform
import subprocess
import re
import logging
from pathlib import Path
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

# Celery integration
from celery_app import celery_app
from tasks import run_agent_loop_task, cancel_run_task, cleanup_stale_runs

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
active_tasks: Dict[str, asyncio.Task] = {}
active_task_orgs: Dict[str, str] = {}
tasks_lock = asyncio.Lock()


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


class FileCreateRequest(BaseModel):
    file_path: str
    content: str = ""
    is_dir: bool = False
    commit_message: Optional[str] = None


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
    if os.getenv("AUTH_DEV_BYPASS", "").strip().lower() == "true":
        logger.warning("AUTH_DEV_BYPASS is enabled - all requests are trusted!")
        return {
            "user_id": os.getenv(
                "AUTH_DEV_USER_ID", "00000000-0000-0000-0000-000000000001"
            ),
            "org_id": os.getenv(
                "AUTH_DEV_ORG_ID", "00000000-0000-0000-0000-000000000002"
            ),
            "email": "dev@localhost",
        }

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

    # Resolve the caller's primary organization membership.
    org_id = None
    try:
        membership = await asyncio.to_thread(
            lambda: supabase.table("organization_members")
            .select("org_id")
            .eq("user_id", user.id)
            .order("joined_at")
            .limit(1)
            .maybe_single()
            .execute()
        )
        if membership and membership.data:
            org_id = membership.data["org_id"]
    except Exception as e:
        logger.warning(f"Failed to resolve org membership for {user.id}: {e}")

    if not org_id:
        raise HTTPException(
            status_code=403,
            detail="No organization membership found. Complete onboarding first.",
        )

    return {"user_id": user.id, "org_id": org_id, "email": user.email}


async def get_org_id(user: dict = Depends(get_current_user)) -> str:
    return user["org_id"]


async def get_user_id(user: dict = Depends(get_current_user)) -> str:
    return user["user_id"]


async def require_repository_access(
    repo_name: str, user: dict = Depends(get_current_user)
) -> dict:
    """Authorize a repository against the caller's organization before access."""
    from agents import supabase as agents_supabase

    if not agents_supabase:
        raise HTTPException(status_code=503, detail="Database not configured")

    repository_id, github_installation_id = await validate_repo_access(
        repo_name, user["org_id"], user["user_id"], agents_supabase
    )
    return {
        "repository_id": repository_id,
        "github_installation_id": github_installation_id,
        "org_id": user["org_id"],
        "user_id": user["user_id"],
    }


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
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail=f"Repository {repo_name} not found in organization. Please sync repositories first.",
        )

    return result.data["id"], result.data["github_installation_id"]


def parse_github_repo_name(repo_url: str) -> str | None:
    """Normalize an owner/repo value or GitHub URL to an owner/repo name."""
    value = repo_url.strip()
    if not value:
        return None
    if "://" in value:
        parsed = urlparse(value)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.netloc.lower() != "github.com"
        ):
            return None
        value = parsed.path.strip("/")
    else:
        value = value.strip("/")
    value = value.removesuffix(".git")
    if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", value):
        return None
    return value


async def receive_terminal_auth(websocket: WebSocket) -> str | None:
    """Receive a browser-compatible bearer-token handshake without using the URL."""
    try:
        message = await asyncio.wait_for(websocket.receive_text(), timeout=10)
    except WebSocketDisconnect:
        return None
    except asyncio.TimeoutError:
        await websocket.close(code=1008, reason="Authentication timeout")
        return None

    try:
        payload = json.loads(message)
    except json.JSONDecodeError:
        await websocket.close(code=1008, reason="Authentication required")
        return None

    token = payload.get("access_token") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "auth"
        or not isinstance(token, str)
        or not token.strip()
    ):
        await websocket.close(code=1008, reason="Authentication required")
        return None
    return f"Bearer {token.strip()}"


def terminal_origin_is_allowed(origin: str | None) -> bool:
    configured_origins = {
        item.strip().rstrip("/")
        for item in os.getenv("ALLOWED_ORIGINS", "").split(",")
        if item.strip()
    }
    allowed_origins = {
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://[::1]:3000",
        *configured_origins,
    }
    return not origin or origin.rstrip("/") in allowed_origins


async def authorize_terminal(
    websocket: WebSocket, repo_url: str, authorization: str | None
) -> str | None:
    """Authenticate a terminal socket and authorize its repository workspace."""
    try:
        user = await get_current_user(authorization)
    except HTTPException:
        await websocket.close(code=1008, reason="Authentication required")
        return None

    repo_name = parse_github_repo_name(repo_url)
    if not repo_name:
        await websocket.close(code=1008, reason="A valid GitHub repository is required")
        return None

    from agents import supabase as agents_supabase

    if not agents_supabase:
        await websocket.close(code=1011, reason="Database not configured")
        return None
    try:
        await validate_repo_access(
            repo_name, user["org_id"], user["user_id"], agents_supabase
        )
    except HTTPException as exc:
        await websocket.close(
            code=1008 if exc.status_code in (401, 403, 404) else 1011,
            reason="Repository access denied",
        )
        return None
    except Exception:
        logger.exception("Failed to authorize terminal repository %s", repo_name)
        await websocket.close(code=1011, reason="Repository authorization failed")
        return None
    return repo_name


@asynccontextmanager
async def lifespan(app: FastAPI):
    global gitnexus_process

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

# Allow the Next.js frontend to connect to this API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
            detail="Supabase is not configured. Please check environment variables.",
        )
    try:
        await asyncio.to_thread(
            lambda: supabase.table("runs").select("id").limit(1).execute()
        )
        return {"status": "healthy", "database": "connected"}
    except Exception as e:
        logger.error(f"Supabase connection check failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=503,
            detail="Supabase connection failed (database may be paused or unreachable)",
        )


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


@app.post("/start")
async def start_agents(
    req: StartRequest,
    org_id: str = Depends(get_org_id),
    user_id: str = Depends(get_user_id),
):
    """
    Start an agent run via Celery task queue.
    Returns immediately with run_id for polling.
    """
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
        raise HTTPException(
            status_code=500, detail=f"Repository validation failed: {e}"
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
        raise HTTPException(status_code=500, detail=f"Failed to create run record: {e}")

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
            kwargs={"target_issue_number": req.target_issue, "mode": req.mode},
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
        raise HTTPException(
            status_code=500, detail=f"Failed to queue agent run: {err_msg}"
        )

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
        .single()
        .execute()
    )

    if not result.data:
        raise HTTPException(status_code=404, detail="Run not found")

    run = result.data

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
    limit: int = 50,
    offset: int = 0,
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


@app.post("/stop")
async def stop_agents(
    req: Optional[StopRequest] = None, org_id: str = Depends(get_org_id)
):
    """Stop/cancel a running agent run."""
    from agents import supabase as agents_supabase

    sb = agents_supabase
    if not sb:
        raise HTTPException(status_code=503, detail="Database not configured")

    if req and req.run_id:
        # Cancel specific run
        run_id = req.run_id

        # Verify ownership
        result = await asyncio.to_thread(
            lambda: sb.table("runs")
            .select("id, status, celery_task_id")
            .eq("id", run_id)
            .eq("org_id", org_id)
            .single()
            .execute()
        )

        if not result.data:
            raise HTTPException(status_code=404, detail="Run not found")

        run = result.data

        if run["status"] not in ("queued", "running"):
            return {
                "status": "not_running",
                "run_id": run_id,
                "current_status": run["status"],
            }

        # Revoke Celery task
        celery_task_id = run.get("celery_task_id")
        if celery_task_id:
            celery_app.control.revoke(celery_task_id, terminate=True, signal="SIGTERM")

        # Update status
        await asyncio.to_thread(
            lambda: sb.table("runs")
            .update(
                {
                    "status": "cancelled",
                    "error_message": "Cancelled by user",
                    "completed_at": datetime.utcnow().isoformat(),
                    "updated_at": datetime.utcnow().isoformat(),
                }
            )
            .eq("id", run_id)
            .execute()
        )

        return {"status": "cancelled", "run_id": run_id}

    else:
        # Cancel all running runs for org
        result = await asyncio.to_thread(
            lambda: sb.table("runs")
            .select("id, celery_task_id")
            .eq("org_id", org_id)
            .in_("status", ["queued", "running"])
            .execute()
        )

        runs = result.data or []
        cancelled_count = 0

        for run in runs:
            celery_task_id = run.get("celery_task_id")
            if celery_task_id:
                celery_app.control.revoke(
                    celery_task_id, terminate=True, signal="SIGTERM"
                )

            await asyncio.to_thread(
                lambda r=run: sb.table("runs")
                .update(
                    {
                        "status": "cancelled",
                        "error_message": "Cancelled by user (bulk)",
                        "completed_at": datetime.utcnow().isoformat(),
                        "updated_at": datetime.utcnow().isoformat(),
                    }
                )
                .eq("id", r["id"])
                .execute()
            )
            cancelled_count += 1

        return {"status": "cancelled", "count": cancelled_count}


# Keep backward compatibility for in-memory tasks (deprecated)
@app.post("/start-legacy")
async def start_agents_legacy(
    req: StartRequest,
    repo_access: dict = Depends(require_repository_access),
):
    """Legacy endpoint using in-memory tasks. Deprecated."""
    run_id = str(uuid.uuid4())
    task = asyncio.create_task(run_agent_loop(req.repo_name, req.target_issue, run_id))

    async with tasks_lock:
        active_tasks[run_id] = task
        active_task_orgs[run_id] = repo_access["org_id"]

    def on_task_done(t):
        async def remove_task():
            async with tasks_lock:
                active_tasks.pop(run_id, None)
                active_task_orgs.pop(run_id, None)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(remove_task())
        except RuntimeError:
            pass

    task.add_done_callback(on_task_done)
    return {"status": "started", "run_id": run_id, "deprecated": True}


@app.post("/stop-legacy")
async def stop_agents_legacy(
    req: Optional[StopRequest] = None,
    user: dict = Depends(get_current_user),
):
    """Legacy endpoint using in-memory tasks. Deprecated."""
    async with tasks_lock:
        if req and req.run_id:
            if active_task_orgs.get(req.run_id) != user["org_id"]:
                raise HTTPException(status_code=404, detail="Run not found")
            task = active_tasks.get(req.run_id)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                active_tasks.pop(req.run_id, None)
                return {"status": "stopped", "run_id": req.run_id}
            return {"status": "not_running", "run_id": req.run_id}
        else:
            stopped_any = False
            for r_id, task in list(active_tasks.items()):
                if active_task_orgs.get(r_id) != user["org_id"]:
                    continue
                if not task.done():
                    task.cancel()
                    stopped_any = True
            for r_id in list(active_tasks):
                if active_task_orgs.get(r_id) == user["org_id"]:
                    active_tasks.pop(r_id, None)
                    active_task_orgs.pop(r_id, None)
            return {"status": "stopped" if stopped_any else "not_running"}


@app.post("/assist/inline")
async def inline_assist(req: InlineAssistRequest):
    from agents import stream_inline_assist

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
        logger.error(f"Inline assist failed: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"Inline assist processing failed: {str(e)}",
        )


# --- File operations (unchanged) ---


@app.post("/repo/{repo_name:path}/file")
async def update_repo_file(
    repo_name: str,
    payload: FileUpdateRequest,
    repo_access: dict = Depends(require_repository_access),
):
    if not re.fullmatch(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)?$", repo_name):
        raise HTTPException(status_code=400, detail="Invalid repository name")
    if (
        not re.fullmatch(r"^[a-zA-Z0-9_.\-/]+$", payload.file_path)
        or ".." in payload.file_path
    ):
        raise HTTPException(status_code=400, detail="Invalid file path")

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(status_code=401, detail="GitHub token not configured")
    gh = Github(token)
    try:
        repo = gh.get_repo(repo_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Repository not found: {e}")
    try:
        file = repo.get_contents(payload.file_path)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found in repo: {e}")
    message = (
        payload.commit_message or f"Update {payload.file_path} via AutoMaintainer IDE"
    )
    try:
        repo.update_file(file.path, message, payload.content, file.sha)

        # Write to local clone so the IDE doesn't show stale reads
        repo_dir = get_safe_repo_dir(repo_name)
        if repo_dir.exists():
            target_path = get_safe_target_path(repo_dir, payload.file_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(payload.content, encoding="utf-8")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update file: {e}")
    return {"status": "updated", "message": message}


@app.post("/repo/{repo_name:path}/file/create")
async def create_repo_file(
    repo_name: str,
    payload: FileCreateRequest,
    repo_access: dict = Depends(require_repository_access),
):
    if not re.fullmatch(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)?$", repo_name):
        raise HTTPException(status_code=400, detail="Invalid repository name")
    if (
        not re.fullmatch(r"^[a-zA-Z0-9_.\-/]+$", payload.file_path)
        or ".." in payload.file_path
    ):
        raise HTTPException(status_code=400, detail="Invalid file path")

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(status_code=401, detail="GitHub token not configured")
    gh = Github(token)
    try:
        repo = gh.get_repo(repo_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Repository not found: {e}")

    message = (
        payload.commit_message or f"Create {payload.file_path} via AutoMaintainer IDE"
    )

    actual_path = payload.file_path
    actual_content = payload.content
    if payload.is_dir:
        actual_path = f"{payload.file_path.rstrip('/')}/.gitkeep"
        actual_content = ""

    try:
        repo.create_file(actual_path, message, actual_content)

        # Write to local clone
        repo_dir = get_safe_repo_dir(repo_name)
        if repo_dir.exists():
            target_path = get_safe_target_path(repo_dir, actual_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(actual_content, encoding="utf-8")

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create file: {e}")
    return {"status": "created", "message": message}


@app.delete("/repo/{repo_name:path}/file")
async def delete_repo_file(
    repo_name: str,
    file_path: str,
    commit_message: Optional[str] = None,
    repo_access: dict = Depends(require_repository_access),
):
    if not re.fullmatch(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)?$", repo_name):
        raise HTTPException(status_code=400, detail="Invalid repository name")
    if not re.fullmatch(r"^[a-zA-Z0-9_.\-/]+$", file_path) or ".." in file_path:
        raise HTTPException(status_code=400, detail="Invalid file path")

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(status_code=401, detail="GitHub token not configured")
    gh = Github(token)
    try:
        repo = gh.get_repo(repo_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Repository not found: {e}")

    try:
        file = repo.get_contents(file_path)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found in repo: {e}")

    if isinstance(file, list):
        raise HTTPException(
            status_code=400,
            detail="Directories cannot be deleted directly via this endpoint.",
        )

    message = commit_message or f"Delete {file_path} via AutoMaintainer IDE"
    try:
        repo.delete_file(file.path, message, file.sha)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete file: {e}")
    return {"status": "deleted", "message": message}


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
    repo_access: dict = Depends(require_repository_access),
):
    """
    Create a new branch, commit changes, and open a Pull Request.
    Used by WebIDE for the preview→PR flow.
    """
    if not re.fullmatch(r"^[a-zA-Z0-9_.-]+(/[a-zA-Z0-9_.-]+)?$", repo_name):
        raise HTTPException(status_code=400, detail="Invalid repository name")

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        raise HTTPException(status_code=401, detail="GitHub token not configured")

    gh = Github(token)
    try:
        repo = gh.get_repo(repo_name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Repository not found: {e}")

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

            if not re.fullmatch(r"^[a-zA-Z0-9_.\-/]+$", file_path) or ".." in file_path:
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
                    repo.create_file(file_path, message, content, branch=branch_name)

        # Create Pull Request
        pr = repo.create_pull(
            title=payload.title,
            body=payload.description or "Changes proposed via AutoMaintainer WebIDE",
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
        raise HTTPException(status_code=500, detail=f"Failed to propose changes: {e}")


# --- Terminal WebSocket (unchanged) ---


@app.websocket("/api/terminal/ws")
async def terminal_ws(websocket: WebSocket, repo_url: str = ""):
    if not terminal_origin_is_allowed(websocket.headers.get("origin")):
        await websocket.close(code=1008, reason="Origin not allowed")
        return

    await websocket.accept()

    authorization = await receive_terminal_auth(websocket)
    if not authorization:
        return

    repo_name = await authorize_terminal(websocket, repo_url, authorization)
    if not repo_name:
        return

    await websocket.send_json({"type": "authenticated"})

    cwd = None
    try:
        repo_dir = get_safe_repo_dir(repo_name)
        base_dir = get_base_workspace_dir()
        real_dir = os.path.realpath(str(repo_dir))
        real_base = os.path.realpath(str(base_dir))
        if (
            real_dir.startswith(real_base + os.path.sep)
            and os.path.exists(real_dir)
            and os.path.isdir(real_dir)
        ):
            cwd = real_dir
    except HTTPException:
        pass

    if sys.platform == "win32":
        import pywinpty

        cols, rows = 80, 24
        pty = pywinpty.PTY(cols, rows)
        pid = pty.spawn(pywinpty.winpty.get_default_cmd(), cwd=cwd)

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
            try:
                pty.close()
            except Exception:
                pass
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

        pid, fd = pty.fork()
        if pid == 0:
            if cwd:
                os.chdir(cwd)
            os.environ["TERM"] = "xterm-256color"
            os.execv("/bin/bash", ["/bin/bash"])
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
    repo_access: dict = Depends(require_repository_access),
):
    repo_dir = get_safe_repo_dir(repo_name)

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
    q: str,
    repo_access: dict = Depends(require_repository_access),
):
    repo_dir = get_safe_repo_dir(repo_name)

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
        raise HTTPException(status_code=500, detail=str(e))

    return {"query": q, "results": results[:100]}


@app.get("/repo/{repo_name:path}/file")
def get_repo_file(
    repo_name: str,
    file_path: str,
    repo_access: dict = Depends(require_repository_access),
):
    repo_dir = get_safe_repo_dir(repo_name)
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


# Serve the static Next.js frontend if the out directory exists
if os.path.exists("../dashboard/out"):
    app.mount(
        "/", StaticFiles(directory="../dashboard/out", html=True), name="dashboard"
    )
elif os.path.exists("dashboard/out"):  # In docker container
    app.mount("/", StaticFiles(directory="dashboard/out", html=True), name="dashboard")


# --- Admin Endpoints ---
# These require admin role (checked via RLS policies in Supabase)


@app.get("/admin/metrics")
async def get_admin_metrics(
    org_id: str = Depends(get_org_id), user_id: str = Depends(get_user_id)
):
    """Get system-wide metrics for admin dashboard."""
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
            .single()
            .execute()
        )
        if not result.data or result.data["role"] not in ("owner", "admin"):
            raise HTTPException(status_code=403, detail="Admin privileges required")

    # Fetch metrics from database views
    try:
        # Org health overview
        org_health = await asyncio.to_thread(
            lambda: sb.table("org_health").select("*").execute()
        )

        # Recent runs
        recent_runs = await asyncio.to_thread(
            lambda: sb.table("recent_runs_detailed")
            .select("*")
            .order("created_at", desc=True)
            .limit(100)
            .execute()
        )

        # Usage summary (last 30 days)
        usage_summary = await asyncio.to_thread(
            lambda: sb.table("monthly_usage_summary")
            .select("*")
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
                "supabaseHealthy": True,  # Would check actual health
                "activeConnections": 0,
                "queueDepth": 0,
            },
        }
    except Exception as e:
        logger.exception(f"Failed to fetch admin metrics: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch metrics: {e}")


@app.get("/admin/runs")
async def get_admin_runs(
    org_id: str = Depends(get_org_id),
    user_id: str = Depends(get_user_id),
    status: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
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
        .single()
        .execute()
    )
    if not result.data or result.data["role"] not in ("owner", "admin"):
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
