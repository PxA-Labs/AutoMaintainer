"""
Celery Application Configuration for AutoMaintainer
Provides durable, scalable task queue for agent runs.
"""
import os
from celery import Celery
from celery.schedules import crontab
from kombu import Queue

# Redis connection
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Celery app
celery_app = Celery(
    "automaintainer",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["tasks"]
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