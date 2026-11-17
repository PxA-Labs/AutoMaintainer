"""Is an agent worker consuming the ``agent_runs`` queue? (#396, #512)

POST /start (main.py) and the issue-label trigger (webhook_processor.py) ask
this before they create a run, so that no run is queued that nobody will
consume. The cached answer and the probe in flight are module state shared by
every caller in the process.

This module must not import FastAPI or main: webhook_processor runs in the
github_sync Celery worker, which should not load the API application to make
one broker round trip (test_worker_probe.py checks the import graph).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os
import re
import threading
import time

from celery_app import celery_app

logger = logging.getLogger(__name__)


def _is_production() -> bool:
    # Same rule as main._is_production; not imported from there (see above).
    return os.getenv("ENVIRONMENT", "development").strip().lower() == "production"


AGENT_QUEUE = "agent_runs"
WORKER_PING_TIMEOUT_SECONDS = float(os.getenv("WORKER_PING_TIMEOUT_SECONDS", "1.0"))
WORKER_PING_CACHE_SECONDS = float(os.getenv("WORKER_PING_CACHE_SECONDS", "5.0"))
# How long /start waits for the probe at most: the connect attempt and the
# reply window are each bounded by the timeout, plus some slack.
WORKER_PROBE_DEADLINE_SECONDS = 2 * WORKER_PING_TIMEOUT_SECONDS + 0.5
_worker_probe = {"checked_at": float("-inf"), "available": False, "future": None}
# Guards _worker_probe only and is never held while a probe runs. Reentrant
# because add_done_callback on a finished future runs the callback at once.
_worker_probe_lock = threading.RLock()
# A single thread: at most one probe in flight, and a probe stuck on the
# network ties up this thread, not one from the pool that the API's
# asyncio.to_thread calls share.
_worker_probe_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="agent-worker-probe"
)
_URL_CREDENTIALS = re.compile(r"(\w+://)[^/@\s]*@")


def _probe_agent_worker() -> bool:
    """True if some live Celery worker consumes the agent_runs queue.

    Uses active_queues rather than ping: on Render the maintenance worker
    (maintenance,github_sync) answers a ping even when no agent worker runs,
    and runs would then sit in the queue forever.

    inspect(timeout=...) bounds only the wait for replies. Connecting to the
    broker would use the transport defaults (seconds of retries when the
    port refuses, the OS TCP connect timeout when the host is unreachable),
    so the probe opens its own connection with short socket timeouts and
    gives up after one retry.
    """
    timeout = WORKER_PING_TIMEOUT_SECONDS
    try:
        with celery_app.connection_for_write(
            transport_options={
                "socket_connect_timeout": timeout,
                "socket_timeout": timeout,
            },
        ) as conn:
            conn.ensure_connection(max_retries=1, timeout=timeout)
            replies = celery_app.control.inspect(
                timeout=timeout, connection=conn
            ).active_queues()
    except Exception as exc:
        # Broker errors can quote the broker URL; never log its password.
        logger.warning(
            "Celery worker probe failed: %s",
            _URL_CREDENTIALS.sub(r"\1***@", str(exc)),
        )
        return False
    return any(
        queue.get("name") == AGENT_QUEUE
        for queues in (replies or {}).values()
        for queue in queues or ()
    )


def _record_worker_probe(future: concurrent.futures.Future) -> None:
    try:
        available = bool(future.result())
    except BaseException:
        available = False
    with _worker_probe_lock:
        _worker_probe.update(
            available=available, checked_at=time.monotonic(), future=None
        )


def _cached_worker_probe() -> "bool | concurrent.futures.Future":
    """The cached answer, or the probe in flight (started if there is none).

    Both answers are cached for WORKER_PING_CACHE_SECONDS: a newly started
    worker is seen within that window, and a burst of /start calls while no
    worker runs does not become a burst of broadcasts. Concurrent callers
    share one probe. Once that probe has outlived the deadline, callers get
    False at once instead of each waiting out the deadline again.
    """
    with _worker_probe_lock:
        now = time.monotonic()
        if now - _worker_probe["checked_at"] < WORKER_PING_CACHE_SECONDS:
            return _worker_probe["available"]
        future = _worker_probe["future"]
        if future is None:
            future = _worker_probe_executor.submit(_probe_agent_worker)
            _worker_probe.update(future=future, started_at=now)
            future.add_done_callback(_record_worker_probe)
        elif now - _worker_probe["started_at"] >= WORKER_PROBE_DEADLINE_SECONDS:
            return False
        return future


# Machine-readable code in the 503 body; `detail` stays a human message.
AGENT_WORKER_UNAVAILABLE = "AGENT_WORKER_UNAVAILABLE"


def agent_worker_check_enabled() -> bool:
    """False when AGENT_WORKER_CHECK is set to a false value.

    For local development and tests that run without a broker. With the
    check off, /start and the trigger label queue runs even when nobody will
    consume them. Read on every call so it can be toggled without a restart.
    """
    value = os.getenv("AGENT_WORKER_CHECK", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


_worker_check_off_warned = False


def _warn_if_check_off_in_production() -> None:
    """Log once per process when production runs with the check disabled."""
    global _worker_check_off_warned
    if _worker_check_off_warned:
        return
    if _is_production():
        _worker_check_off_warned = True
        logger.warning(
            "AGENT_WORKER_CHECK is off with ENVIRONMENT=production: runs are "
            "queued even when no agent worker will consume them"
        )


async def agent_worker_available() -> bool:
    """True if a live worker consumes agent_runs, or the check is off.

    Waits at most WORKER_PROBE_DEADLINE_SECONDS. A broker error, no reply
    or a probe past its deadline is False.
    """
    if not agent_worker_check_enabled():
        _warn_if_check_off_in_production()
        return True
    probe = _cached_worker_probe()
    if isinstance(probe, bool):
        return probe
    # Awaited on the event loop, so a slow broker holds no pool thread. The
    # shield stops a caller that times out from cancelling the shared probe.
    # Each caller waits only for what is left of the shared probe's deadline,
    # so a late joiner never waits a second full deadline (a timer firing a
    # few ms early must not let the next caller start a fresh wait).
    with _worker_probe_lock:
        started_at = _worker_probe["started_at"]
    remaining = WORKER_PROBE_DEADLINE_SECONDS - (time.monotonic() - started_at)
    try:
        return await asyncio.wait_for(
            asyncio.shield(asyncio.wrap_future(probe)),
            max(0.0, remaining),
        )
    except asyncio.TimeoutError:
        return False
