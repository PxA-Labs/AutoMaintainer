"""
Celery Application Configuration for AutoMaintainer
Provides durable, scalable task queue for agent runs.

Worker auto-restart has two layers:
1. Celery-level (configured below): worker_max_tasks_per_child and
   worker_max_memory_per_child recycle a *child process* after it's
   done too much work or grown too large -- guards against memory
   leaks in long agent runs.
2. Process-supervisor level (outside this file): if the whole worker
   *daemon* crashes, something outside Python needs to bring it back
   -- e.g. `Restart=always` in a systemd unit, or a Docker/Kubernetes
   restart policy. That's infra config, not something celery_app.py
   can express.
"""

import os
from urllib.parse import urlparse

try:
    from celery import Celery
    from celery.schedules import crontab
    from kombu import Queue

    CELERY_AVAILABLE = True
except ImportError:
    CELERY_AVAILABLE = False

    class _MockConf(dict):
        def update(self, *args, **kwargs):
            pass

    class Celery:
        def __init__(self, *args, **kwargs):
            self.conf = _MockConf()
            self.control = type("Control", (), {"revoke": lambda *a, **k: None})()

        def config_from_object(self, *args, **kwargs):
            pass

        def autodiscover_tasks(self, *args, **kwargs):
            pass

        def task(self, *args, **kwargs):
            return lambda fn: fn

        def AsyncResult(self, *args, **kwargs):
            return type(
                "Result",
                (),
                {"status": "PENDING", "result": None, "ready": lambda: False},
            )()

        def start(self, *args, **kwargs):
            pass

    def crontab(*args, **kwargs):
        return None

    def Queue(name, routing_key=None):
        return name


# Redis connection
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def redis_configured() -> bool:
    """
    True only when a real REDIS_URL was provided via the environment.
    Used by start_agent_task/cancel_agent_task in tasks.py to decide
    between queuing through Celery and running inline -- the
    "zero-overhead fallback for local single-process development"
    acceptance criterion. We deliberately don't default this to the
    localhost fallback URL above: an unset REDIS_URL should mean
    "no broker configured", not "assume localhost is running".
    """
    return bool(os.getenv("REDIS_URL"))


def _redis_ssl_options():
    """
    Celery/kombu expects SSL options as a dict (or None to disable).
    We only enable them when the URL scheme is rediss:// -- e.g.
    managed Redis providers (AWS ElastiCache, Upstash, Redis Cloud)
    that require TLS in production.
    """
    if urlparse(REDIS_URL).scheme != "rediss":
        return None
    return {
        "ssl_cert_reqs": os.getenv("REDIS_SSL_CERT_REQS", "required"),
    }


# Celery app
celery_app = Celery(
    "automaintainer", broker=REDIS_URL, backend=REDIS_URL, include=["tasks"]
)

# Celery configuration
celery_app.conf.update(
    # Task serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    # Timezone
    timezone="UTC",
    enable_utc=True,
    # SSL support -- only applied when REDIS_URL uses rediss://
    broker_use_ssl=_redis_ssl_options(),
    redis_backend_use_ssl=_redis_ssl_options(),
    # Connection pooling -- caps concurrent broker connections per worker
    # process instead of opening a fresh one per task.
    broker_pool_limit=int(os.getenv("CELERY_BROKER_POOL_LIMIT", "10")),
    broker_connection_retry_on_startup=True,
    # Task routing
    task_routes={
        "tasks.run_agent_loop_task": {"queue": "agent_runs"},
        "tasks.cleanup_stale_runs": {"queue": "maintenance"},
        "tasks.sync_repositories": {"queue": "github_sync"},
    },
    # Queue definitions
    task_queues=(
        Queue("agent_runs", routing_key="agent_runs"),
        Queue("maintenance", routing_key="maintenance"),
        Queue("github_sync", routing_key="github_sync"),
        Queue("default", routing_key="default"),
    ),
    # Worker configuration
    worker_prefetch_multiplier=1,  # One task per worker for long-running agent runs
    worker_max_tasks_per_child=10,  # Restart worker after 10 tasks to prevent memory leaks
    worker_max_memory_per_child=512_000,  # Restart worker if it exceeds ~512MB RSS
    worker_disable_rate_limits=False,
    # Task execution
    task_acks_late=True,  # Acknowledge after completion (not before)
    task_reject_on_worker_lost=True,  # Requeue if worker dies
    task_track_started=True,  # Track when task starts
    # Result backend
    result_expires=86400,  # 24 hours
    result_compression="gzip",
    # Beat schedule (periodic tasks)
    beat_schedule={
        "cleanup-stale-runs": {
            "task": "tasks.cleanup_stale_runs",
            "schedule": crontab(minute="*/5"),  # Every 5 minutes
        },
        "sync-repositories": {
            "task": "tasks.sync_repositories",
            "schedule": crontab(hour="*/6"),  # Every 6 hours
        },
    },
    # Monitoring
    worker_send_task_events=True,
    task_send_sent_event=True,
)

# Auto-discover tasks
celery_app.autodiscover_tasks(["backend"])

if __name__ == "__main__":
    celery_app.start()
