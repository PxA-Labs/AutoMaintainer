"""
Celery Tasks for AutoMaintainer
Durable, scalable task execution for agent runs and maintenance.

Schema dependency: cancel_agent_task() revokes the exact running Celery
task and release_repo_lock() frees the distributed lock, both of which
need the `runs` table to have a `celery_task_id` column (text, nullable)
in addition to the existing `repository_id`. If that column doesn't
exist yet, add it in a migration before deploying this.
"""

import os
import asyncio
import threading
import uuid
from datetime import datetime, timedelta
from typing import Optional
import logging

from redis_locks import (
    acquire_repo_lock,
    release_repo_lock,
    is_redis_available,
    write_heartbeat,
)

try:
    from celery import shared_task
    from celery.exceptions import SoftTimeLimitExceeded
    from celery.signals import task_prerun, task_postrun, task_failure, task_retry

    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False

    def shared_task(*args, **kwargs):
        def decorator(fn):
            fn.delay = fn
            fn.apply_async = fn
            return fn

        if len(args) == 1 and callable(args[0]):
            return decorator(args[0])
        return decorator

    class SoftTimeLimitExceeded(Exception):
        pass

    class _MockSignal:
        def connect(self, fn):
            return fn

    task_prerun = _MockSignal()
    task_postrun = _MockSignal()
    task_failure = _MockSignal()
    task_retry = _MockSignal()

from supabase import create_client, Client

from agents import run_agent_loop, broadcast_log

logger = logging.getLogger(__name__)

# Supabase client for tasks (service role)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

task_supabase: Client | None = None
if SUPABASE_URL and SUPABASE_SERVICE_KEY:
    task_supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def get_supabase() -> Client:
    """Get Supabase client, initializing if needed."""
    global task_supabase
    if task_supabase:
        return task_supabase
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url:
        raise RuntimeError("SUPABASE_URL is required for worker persistence")
    if not key:
        raise RuntimeError(
            "SUPABASE_SERVICE_KEY is required for worker persistence; "
            "anonymous keys are not supported"
        )
    task_supabase = create_client(url, key)
    return task_supabase


async def update_run_status(run_id: str, status: str, **kwargs) -> None:
    """Update run status in database."""
    sb = get_supabase()
    update_data = {
        "status": status,
        "updated_at": datetime.utcnow().isoformat(),
        **kwargs,
    }
    if status == "running" and "started_at" not in kwargs:
        update_data["started_at"] = datetime.utcnow().isoformat()
    if status in ("completed", "failed", "cancelled"):
        update_data["completed_at"] = datetime.utcnow().isoformat()

    try:
        await asyncio.to_thread(
            lambda: sb.table("runs").update(update_data).eq("id", run_id).execute()
        )
    except Exception as e:
        logger.error(f"Failed to update run {run_id} status: {e}")


async def log_run_event(
    run_id: str,
    org_id: str,
    agent_name: str,
    message: str,
    log_type: str = "message",
    color: str = "text-zinc-400",
    metadata: dict = None,
) -> None:
    """Log an event for a run."""
    sb = get_supabase()
    try:
        await asyncio.to_thread(
            lambda: sb.table("logs")
            .insert(
                {
                    "run_id": run_id,
                    "org_id": org_id,
                    "agent_name": agent_name,
                    "log_type": log_type,
                    "message": message,
                    "color": color,
                    "metadata": metadata or {},
                }
            )
            .execute()
        )
    except Exception as e:
        logger.error(f"Failed to log event for run {run_id}: {e}")


async def record_usage_event(
    org_id: str,
    event_type: str,
    user_id: str = None,
    run_id: str = None,
    quantity: int = 1,
    unit: str = "count",
    estimated_cost_cents: int = 0,
    metadata: dict = None,
    model_used: str = None,
) -> None:
    """Record a usage event for billing."""
    sb = get_supabase()
    try:
        await asyncio.to_thread(
            lambda: sb.table("usage_events")
            .insert(
                {
                    "org_id": org_id,
                    "user_id": user_id,
                    "run_id": run_id,
                    "event_type": event_type,
                    "quantity": quantity,
                    "unit": unit,
                    "estimated_cost_cents": estimated_cost_cents,
                    "metadata": metadata or {},
                    "model_used": model_used,
                    "provider": "groq",
                }
            )
            .execute()
        )
    except Exception as e:
        logger.error(f"Failed to record usage event: {e}")


@shared_task(
    bind=True,
    name="tasks.run_agent_loop_task",
    queue="agent_runs",
    soft_time_limit=1800,  # 30 minutes soft limit
    time_limit=1920,  # 32 minutes hard limit
    max_retries=2,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=300,
    retry_jitter=True,
)
def run_agent_loop_task(
    self,
    run_id: str,
    repo_name: str,
    org_id: str,
    user_id: str,
    repository_id: int,
    github_installation_id: int,
    target_issue_number: Optional[int] = None,
    mode: str = "autonomous",
):
    """
    Celery task to run the agent loop.
    This replaces the in-memory asyncio.Task approach.
    """
    # Distributed lock: refuse to run if another run already owns this
    # repository. self.retry() here (instead of just failing) means a
    # queued run waits its turn rather than erroring out.
    if not acquire_repo_lock(repository_id, run_id):
        logger.info(
            f"Repo {repo_name} (id={repository_id}) is locked by another run; "
            f"retrying run {run_id} later"
        )
        raise self.retry(countdown=30, max_retries=60)

    # Heartbeat thread: proves this specific task is alive in near-real-time,
    # independent of how often the agent loop itself writes to the DB.
    # cleanup_stale_runs (or any external monitor) can check this Redis key
    # instead of relying solely on infrequent DB timestamp updates.
    heartbeat_stop = threading.Event()

    def _heartbeat_loop():
        while not heartbeat_stop.wait(20):
            write_heartbeat(run_id)

    heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    heartbeat_thread.start()

    # Create new event loop for this task
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        logger.info(f"Starting agent run {run_id} for repo {repo_name}")

        # Update status to running, and record the Celery task id so
        # cancel_agent_task() can later call control.revoke() on the
        # exact task, not just flip a DB flag.
        loop.run_until_complete(
            update_run_status(run_id, "running", celery_task_id=self.request.id)
        )

        # Record usage event
        loop.run_until_complete(
            record_usage_event(
                org_id=org_id,
                user_id=user_id,
                run_id=run_id,
                event_type="agent_run_started",
                metadata={"repo_name": repo_name, "mode": mode},
            )
        )

        # Log start
        loop.run_until_complete(
            log_run_event(
                run_id=run_id,
                org_id=org_id,
                agent_name="System",
                message=f"Agent loop started for {repo_name} (mode: {mode})",
                color="text-emerald-500",
            )
        )

        # Run the actual agent loop
        # The run_agent_loop function from agents.py handles the LangGraph execution
        result = loop.run_until_complete(
            run_agent_loop(
                repo_name=repo_name, target_issue=target_issue_number, run_id=run_id
            )
        )

        # Guard against legacy/None returns so status handling never crashes
        if not isinstance(result, dict):
            result = {
                "status": "failed",
                "error": f"Agent loop returned invalid result: {result!r}",
            }

        # Update status based on result
        if result.get("status") == "completed":
            loop.run_until_complete(
                update_run_status(
                    run_id,
                    "completed",
                    result_summary=result.get("summary"),
                    github_issue_number=result.get("issue_number"),
                    github_pr_number=result.get("pr_number"),
                    github_branch_name=result.get("branch_name"),
                    github_commit_sha=result.get("commit_sha"),
                )
            )

            loop.run_until_complete(
                record_usage_event(
                    org_id=org_id,
                    user_id=user_id,
                    run_id=run_id,
                    event_type="agent_run_completed",
                    metadata={"repo_name": repo_name},
                )
            )

            loop.run_until_complete(
                log_run_event(
                    run_id=run_id,
                    org_id=org_id,
                    agent_name="System",
                    message="Agent loop completed successfully",
                    color="text-emerald-500",
                )
            )
        else:
            error_msg = result.get("error", "Unknown error")
            loop.run_until_complete(
                update_run_status(run_id, "failed", error_message=error_msg)
            )

            loop.run_until_complete(
                record_usage_event(
                    org_id=org_id,
                    user_id=user_id,
                    run_id=run_id,
                    event_type="agent_run_failed",
                    metadata={"repo_name": repo_name, "error": error_msg},
                )
            )

            loop.run_until_complete(
                log_run_event(
                    run_id=run_id,
                    org_id=org_id,
                    agent_name="System",
                    message=f"Agent loop failed: {error_msg}",
                    color="text-red-500",
                )
            )

        return result

    except SoftTimeLimitExceeded:
        logger.warning(f"Run {run_id} exceeded soft time limit")
        loop.run_until_complete(
            update_run_status(
                run_id, "failed", error_message="Execution timeout (30 minutes)"
            )
        )
        loop.run_until_complete(
            log_run_event(
                run_id=run_id,
                org_id=org_id,
                agent_name="System",
                message="Agent loop timed out after 30 minutes",
                color="text-red-500",
            )
        )
        raise

    except Exception as e:
        logger.exception(f"Run {run_id} failed with exception: {e}")
        loop.run_until_complete(
            update_run_status(run_id, "failed", error_message=str(e))
        )
        loop.run_until_complete(
            log_run_event(
                run_id=run_id,
                org_id=org_id,
                agent_name="System",
                message=f"Agent loop crashed: {str(e)}",
                color="text-red-500",
            )
        )
        raise

    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        release_repo_lock(repository_id, run_id)
        loop.close()


@shared_task(
    bind=True,
    name="tasks.cleanup_stale_runs",
    queue="maintenance",
    soft_time_limit=300,
)
def cleanup_stale_runs(self):
    """
    Periodic task to clean up runs stuck in 'running' or 'queued' state.
    Runs that haven't been updated in 10+ minutes are marked as failed.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        sb = get_supabase()
        cutoff = (datetime.utcnow() - timedelta(minutes=10)).isoformat()

        # Find stale runs
        result = loop.run_until_complete(
            asyncio.to_thread(
                lambda: sb.table("runs")
                .select("id, org_id, repo_name")
                .in_("status", ["running", "queued"])
                .lt("updated_at", cutoff)
                .execute()
            )
        )

        stale_runs = result.data or []
        cleaned = 0

        for run in stale_runs:
            run_id = run["id"]
            org_id = run["org_id"]
            repo_name = run.get("repo_name", "unknown")

            logger.info(f"Cleaning up stale run {run_id} for {repo_name}")

            loop.run_until_complete(
                update_run_status(
                    run_id,
                    "failed",
                    error_message="Stale run cleaned up (no heartbeat for 10+ minutes)",
                )
            )

            loop.run_until_complete(
                log_run_event(
                    run_id=run_id,
                    org_id=org_id,
                    agent_name="System",
                    message="Run marked as failed: stale (no updates for 10+ minutes)",
                    color="text-amber-500",
                )
            )

            cleaned += 1

        logger.info(f"Cleaned up {cleaned} stale runs")
        return {"cleaned": cleaned, "run_ids": [r["id"] for r in stale_runs]}

    except Exception as e:
        logger.exception(f"Cleanup stale runs failed: {e}")
        raise
    finally:
        loop.close()


@shared_task(
    bind=True,
    name="tasks.sync_repositories",
    queue="github_sync",
    soft_time_limit=600,
)
def sync_repositories(self):
    """
    Periodic task to sync repositories from GitHub for all active installations.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        sb = get_supabase()

        # Get all active installations
        result = loop.run_until_complete(
            asyncio.to_thread(
                lambda: sb.table("github_installations")
                .select("id, org_id, account_login, access_token")
                .is_("suspended_at", "null")
                .execute()
            )
        )

        installations = result.data or []
        synced_count = 0

        for install in installations:
            # This would use GitHub App token to fetch repos
            # Implementation depends on GitHub App integration (next task)
            logger.info(
                f"Would sync repos for installation {install['id']} ({install['account_login']})"
            )
            synced_count += 1

        return {"installations_processed": synced_count}

    except Exception as e:
        logger.exception(f"Sync repositories failed: {e}")
        raise
    finally:
        loop.close()


@shared_task(
    bind=True,
    name="tasks.cancel_run_task",
    queue="agent_runs",
)
def cancel_run_task(self, run_id: str):
    """
    Cancel a running agent run by updating its status.
    The agent loop should check status periodically and exit gracefully.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        sb = get_supabase()

        # Get run details, including the fields needed to actually stop
        # the worker (celery_task_id) and free the repo lock (repository_id)
        result = loop.run_until_complete(
            asyncio.to_thread(
                lambda: sb.table("runs")
                .select("org_id, status, repository_id, celery_task_id")
                .eq("id", run_id)
                .single()
                .execute()
            )
        )

        if not result.data:
            return {"success": False, "error": "Run not found"}

        run = result.data
        if run["status"] not in ("queued", "running"):
            return {
                "success": False,
                "error": f"Run not cancellable (status: {run['status']})",
            }

        # Actually stop the Celery worker task, not just flag the DB.
        # SIGTERM lets the task's `finally` block run (lock release,
        # heartbeat thread stop) before the process is recycled.
        celery_task_id = run.get("celery_task_id")
        if celery_task_id:
            from celery_app import celery_app  # local import avoids circular import

            celery_app.control.revoke(celery_task_id, terminate=True, signal="SIGTERM")

        # Update status
        loop.run_until_complete(
            update_run_status(run_id, "cancelled", error_message="Cancelled by user")
        )

        loop.run_until_complete(
            log_run_event(
                run_id=run_id,
                org_id=run["org_id"],
                agent_name="System",
                message="Run cancelled by user",
                color="text-amber-500",
            )
        )

        # Free the repo lock immediately rather than waiting for the
        # terminated task's own `finally` block, which may not run
        # reliably under a hard SIGTERM.
        if run.get("repository_id"):
            release_repo_lock(run["repository_id"], run_id)

        return {"success": True}

    except Exception as e:
        logger.exception(f"Cancel run failed: {e}")
        return {"success": False, "error": str(e)}
    finally:
        loop.close()


def start_agent_task(run_id: str, config_payload: dict) -> dict:
    """
    Entry point matching the Epic #158 spec: start_agent_task(run_id, config_payload).

    config_payload is expected to contain: repo_name, org_id, user_id,
    repository_id, github_installation_id, and optionally
    target_issue_number, mode.

    Decoupling from the FastAPI handler: when Redis is configured and
    reachable, this enqueues onto the agent_runs queue and returns
    immediately with a task_id -- the actual agent loop executes in a
    separate Celery worker process. When Redis is NOT configured (local
    single-process dev), it falls back to calling the same task function
    directly, which Celery executes eagerly in-process with zero queue
    overhead, so `/start` still works without standing up Redis.
    """
    kwargs = dict(
        run_id=run_id,
        repo_name=config_payload["repo_name"],
        org_id=config_payload["org_id"],
        user_id=config_payload["user_id"],
        repository_id=config_payload["repository_id"],
        github_installation_id=config_payload["github_installation_id"],
        target_issue_number=config_payload.get("target_issue_number"),
        mode=config_payload.get("mode", "autonomous"),
    )

    if CELERY_AVAILABLE and is_redis_available():
        async_result = run_agent_loop_task.delay(**kwargs)
        return {"success": True, "queued": True, "task_id": async_result.id}

    # Zero-overhead fallback: calling the task object directly (instead of
    # .delay()) runs it synchronously in this process -- no broker needed.
    result = run_agent_loop_task(**kwargs)
    return {"success": True, "queued": False, "result": result}


def cancel_agent_task(run_id: str) -> dict:
    """
    Entry point matching the Epic #158 spec: cancel_agent_task(run_id).
    Thin wrapper -- cancel_run_task already does the real work (revoke
    the Celery task, update DB status to 'cancelled', release the lock).
    Kept as a separate name so callers can depend on the spec's exact
    function name without caring whether cancellation is itself a task.
    """
    return cancel_run_task(run_id)


# Celery signal handlers for monitoring


@task_prerun.connect
def task_prerun_handler(
    sender=None, task_id=None, task=None, args=None, kwargs=None, **kwds
):
    logger.info(f"Task started: {task.name}[{task_id}]")


@task_postrun.connect
def task_postrun_handler(
    sender=None,
    task_id=None,
    task=None,
    args=None,
    kwargs=None,
    retval=None,
    state=None,
    **kwds,
):
    logger.info(f"Task finished: {task.name}[{task_id}] state={state}")


@task_failure.connect
def task_failure_handler(
    sender=None, task_id=None, exception=None, traceback=None, einfo=None, **kwds
):
    logger.error(f"Task failed: {sender.name}[{task_id}] - {exception}")


@task_retry.connect
def task_retry_handler(sender=None, task_id=None, reason=None, einfo=None, **kwds):
    logger.warning(f"Task retry: {sender.name}[{task_id}] - {reason}")
