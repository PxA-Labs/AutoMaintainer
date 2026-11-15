"""
Processing of stored GitHub webhook deliveries (#391).

POST /webhooks/github verifies a delivery and stores it in ``webhook_events``
with status 'pending' (github_app.handle_github_webhook). This module turns
those rows into state changes:

- ``installation``: ``deleted`` and ``suspend`` set the installation's
  ``suspended_at``, which the periodic repository sync already skips;
  ``unsuspend`` clears it and re-syncs the installation's repositories.
- ``installation_repositories`` (``added`` / ``removed``): re-sync that
  installation's repositories through github_app.sync_installation_repositories.
- Everything else is marked 'ignored' explicitly, so nothing stays pending.

Deleted installations are deactivated, not deleted: repositories, runs and
webhook_events all reference github_installations with ON DELETE CASCADE,
so a hard delete would erase the org's run history and this very event row.

Idempotency. A row is claimed with a conditional update
(status 'pending' -> 'processing'); only the worker whose update matched the
row processes it, so duplicate task deliveries and overlapping sweeps are
no-ops. Terminal states ('completed', 'ignored', final 'failed') set
``processed_at``. A failure records ``error_message`` and increments
``retry_count``; the row goes back to 'pending' until ``max_retries`` is
reached, and the periodic sweep picks it up again. The handlers themselves
are safe to repeat (setting a timestamp, upserting repositories).

Not handled: a row left in 'processing' by a worker that died mid-event
stays there; it needs manual requeueing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from celery_app import celery_app
from github_app import sync_installation_repositories

logger = logging.getLogger(__name__)

TABLE = "webhook_events"
SWEEP_BATCH_SIZE = 50
ERROR_MESSAGE_MAX_LENGTH = 1000

DEACTIVATING_ACTIONS = {"deleted", "suspend"}
REPOSITORY_ACTIONS = {"added", "removed"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Ignored(Exception):
    """The event needs no action; recorded as status 'ignored'."""


def _get_installation(sb, installation_id, org_id) -> Optional[dict]:
    result = (
        sb.table("github_installations")
        .select("id, org_id, suspended_at")
        .eq("id", installation_id)
        .eq("org_id", org_id)
        .limit(1)
        .maybe_single()
        .execute()
    )
    return getattr(result, "data", None) if result is not None else None


def _set_suspended_at(sb, installation_id, org_id, value: Optional[str]) -> None:
    (
        sb.table("github_installations")
        .update({"suspended_at": value, "updated_at": _now()})
        .eq("id", installation_id)
        .eq("org_id", org_id)
        .execute()
    )


def _sync(sb, installation_id, org_id) -> None:
    synced = asyncio.run(sync_installation_repositories(sb, installation_id, org_id))
    logger.info(
        "Webhook re-synced %s repositories for installation %s",
        synced,
        installation_id,
    )


def _handle(sb, event: dict) -> None:
    """Apply one event. Raises Ignored when there is nothing to do."""
    event_type = event.get("event_type")
    action = event.get("action")
    installation_id = event["github_installation_id"]
    org_id = event["org_id"]

    if event_type == "installation":
        if action in DEACTIVATING_ACTIONS:
            _set_suspended_at(sb, installation_id, org_id, _now())
            return
        if action == "unsuspend":
            _set_suspended_at(sb, installation_id, org_id, None)
            _sync(sb, installation_id, org_id)
            return
        raise Ignored(f"installation.{action} needs no action")

    if event_type == "installation_repositories":
        if action not in REPOSITORY_ACTIONS:
            raise Ignored(f"installation_repositories.{action} needs no action")
        installation = _get_installation(sb, installation_id, org_id)
        if not installation:
            raise Ignored(f"installation {installation_id} is not registered")
        if installation.get("suspended_at"):
            raise Ignored(f"installation {installation_id} is suspended")
        _sync(sb, installation_id, org_id)
        return

    raise Ignored(f"{event_type} events are not processed")


def _claim(sb, event: dict) -> bool:
    """Atomically move a pending row to 'processing'; False if already taken."""
    result = (
        sb.table(TABLE)
        .update({"status": "processing"})
        .eq("id", event["id"])
        .eq("status", "pending")
        .execute()
    )
    return bool(result.data)


def _finish(sb, event_id, fields: dict) -> None:
    sb.table(TABLE).update(fields).eq("id", event_id).execute()


def process_event(sb, event: dict) -> str:
    """Process one stored event; return its resulting status or 'skipped'."""
    if event.get("processed_at") or event.get("status") != "pending":
        return "skipped"
    if not _claim(sb, event):
        return "skipped"

    try:
        _handle(sb, event)
    except Ignored as reason:
        logger.debug("Webhook %s ignored: %s", event.get("github_delivery_id"), reason)
        _finish(sb, event["id"], {"status": "ignored", "processed_at": _now()})
        return "ignored"
    except Exception as exc:
        retries = (event.get("retry_count") or 0) + 1
        final = retries >= (event.get("max_retries") or 3)
        logger.exception(
            "Webhook %s failed (attempt %s)", event.get("github_delivery_id"), retries
        )
        fields = {
            "status": "failed" if final else "pending",
            "retry_count": retries,
            "error_message": f"{type(exc).__name__}: {exc}"[:ERROR_MESSAGE_MAX_LENGTH],
        }
        if final:
            fields["processed_at"] = _now()
        _finish(sb, event["id"], fields)
        return fields["status"]

    _finish(
        sb,
        event["id"],
        {"status": "completed", "processed_at": _now(), "error_message": None},
    )
    return "completed"


def process_pending_events(
    sb, delivery_id: Optional[str] = None, limit: int = SWEEP_BATCH_SIZE
) -> dict:
    """Process pending rows, oldest first; only ``delivery_id`` if given."""
    query = (
        sb.table(TABLE).select("*").eq("status", "pending").is_("processed_at", "null")
    )
    if delivery_id:
        query = query.eq("github_delivery_id", delivery_id)
    rows = query.order("received_at").limit(limit).execute().data or []

    counts: dict[str, int] = {}
    for event in rows:
        try:
            status = process_event(sb, event)
        except Exception:
            # Claiming or recording failed (database error): leave the row for
            # the next sweep rather than abort the whole batch.
            logger.exception("Could not record webhook event %s", event.get("id"))
            status = "error"
        counts[status] = counts.get(status, 0) + 1
    return counts


@celery_app.task(name="webhook_processor.process_webhook_events")
def process_webhook_events_task(delivery_id: Optional[str] = None) -> dict:
    """Celery entry point: one delivery (enqueued by the webhook route) or,
    with no argument, a sweep of every pending row (beat schedule)."""
    from tasks import get_supabase

    return process_pending_events(get_supabase(), delivery_id=delivery_id)
