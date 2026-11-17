"""
Processing of stored GitHub webhook deliveries (#391).

POST /webhooks/github verifies a delivery and stores it in ``webhook_events``
with status 'pending' (github_app.handle_github_webhook). This module turns
those rows into state changes:

- ``installation``: ``deleted`` and ``suspend`` set the installation's
  ``suspended_at`` (GitHub's timestamp when the payload has one) and drop
  this process's cached tokens for it. The periodic repository sync skips
  such installations and github_app refuses to mint tokens for them.
  ``unsuspend`` clears it and re-syncs the installation's repositories.
- ``installation_repositories`` (``added`` / ``removed``): re-sync that
  installation's repositories through github_app.sync_installation_repositories.
- ``issues.labeled`` with the trigger label (AUTOMAINTAINER_TRIGGER_LABEL):
  starts an agent run on that issue; see "Label trigger" below.
- Everything else is marked 'ignored' explicitly, so nothing stays pending.
  That includes ``issues.opened`` and ``pull_request.closed``, which #190
  lists: no product behaviour depends on them today. A run starts from the
  label, not from a new issue, and a run follows its own PR (merge or human
  review), not the PR's close event.

Label trigger (#190). When a label equal to AUTOMAINTAINER_TRIGGER_LABEL
(compared case-insensitively, as GitHub compares label names; unset or empty
turns the feature off) is added to an open issue of a registered repository,
a run is created and queued as POST /start does: the same ``runs`` row and
the same ``run_agent_loop_task`` arguments, with ``target_issue_number`` set.
The run's trigger is the label's adder (the delivery's ``sender``), by
numeric GitHub user id, with ``trigger_source`` "label". The trusted-trigger
gate (trust.py) therefore decides the merge exactly as for /start: under
MERGE_POLICY ci or auto the PR is merged only when both the adder and the
issue's author can write to the repository; otherwise it stays open for a
human.

Who gets a run. Only users with triage access or more can add a label to an
existing issue, so the adder is a repository insider, though not
necessarily a writer. Such a run starts and opens a PR, and the gate keeps
an untrusted adder's PR from being merged (#411: no merge without a trusted
human; a PR is the safe default). Two cases get no run at all, because no
insider chose to spend one:

- a bot adder (an Actions workflow or another App, possibly reacting to an
  outsider's event): nothing identifies the human behind it, and the gate
  would never trust it anyway;
- an adder who is the issue's own author and not an OWNER, MEMBER or
  COLLABORATOR (the issue's ``author_association``): GitHub applies an issue
  template's labels on the author's behalf when the issue is opened, so a
  template carrying the trigger label would otherwise let any outsider start
  runs (#411: act only on a maintainer-applied label).

The run's ``user_id`` and trigger. ``runs.user_id`` is a non-null
AutoMaintainer user, and a label adder need not have an AutoMaintainer
account. The run is attributed to the org's first owner, the account that
answers for the installation. Who actually started it is recorded in the
row's trigger columns (``trigger_source`` "label", ``trigger_github_id``,
``trigger_login``; POST /start fills them with "api" and the caller's GitHub
identity) and in the run's log. ``user_id`` stays non-null: the runs RLS
policies, the ``user_display_name`` of recent_runs_detailed and the usage
rows all key on it, and the trigger columns already say who it was.

Guards, each recorded as 'ignored' with a log line: the repository must be
registered to the event's org under the event's installation, and that
installation must not be suspended; no run may be queued, running or
awaiting review for the same (repository, issue); and an agent worker must
be available (worker_probe, the check POST /start makes, #512), so no run
is created that nobody consumes. A label added while no worker is available
is not remembered: it must be removed and added again once a worker runs.
GitHub needs no error for any of these. A redelivery (same
X-GitHub-Delivery) is never stored twice (github_app.handle_github_webhook),
and a stored row is processed once (see Idempotency).

One active run per issue (#518). The active-run check above is only the
cheap path. The database enforces the rule: the partial unique index
``runs_one_active_per_issue`` (repository_id, target_issue_number) over
queued, running and awaiting_review runs rejects a second active run for the
same issue, so two deliveries processed at the same moment (the github_sync
worker with concurrency above 1, a label removed and re-added quickly)
cannot both start one. The insert that loses is recorded as 'ignored', with
no run row and no Celery task. POST /start answers 409 in the same case.

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
are safe to repeat (setting a timestamp, upserting repositories; a label
retried after its run was queued finds that run active and is ignored).

Not handled: a row left in 'processing' by a worker that died mid-event
stays there; it needs manual requeueing.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import worker_probe
from celery_app import celery_app
from github_app import invalidate_installation_tokens, sync_installation_repositories

logger = logging.getLogger(__name__)

TABLE = "webhook_events"
SWEEP_BATCH_SIZE = 50
ERROR_MESSAGE_MAX_LENGTH = 1000

DEACTIVATING_ACTIONS = {"deleted", "suspend"}
REPOSITORY_ACTIONS = {"added", "removed"}

TRIGGER_LABEL_ENV = "AUTOMAINTAINER_TRIGGER_LABEL"
TRIGGER_SOURCE = "label"
# The runs.status values outside tasks.TERMINAL_RUN_STATUSES.
ACTIVE_RUN_STATUSES = ("queued", "running", "awaiting_review")
# Partial unique index over runs in ACTIVE_RUN_STATUSES with a target issue
# (supabase/migrations/20261117140000_run_trigger_and_active_issue.sql).
ACTIVE_RUN_INDEX = "runs_one_active_per_issue"
UNIQUE_VIOLATION = "23505"
# author_association values of people the repository's owner let in.
INSIDER_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


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


def trigger_label() -> Optional[str]:
    """The configured trigger label, or None when the feature is off."""
    value = (os.getenv(TRIGGER_LABEL_ENV) or "").strip()
    return value or None


def is_active_run_conflict(exc: BaseException) -> bool:
    """True if ``exc`` is the database refusing a second active run for an
    issue (unique violation of ACTIVE_RUN_INDEX). postgrest-py's APIError
    carries the SQLSTATE in ``code`` and the index name in ``message``."""
    if str(getattr(exc, "code", "") or "") != UNIQUE_VIOLATION:
        return False
    text = f"{getattr(exc, 'message', '') or ''} {getattr(exc, 'details', '') or ''}"
    return ACTIVE_RUN_INDEX in f"{text} {exc}"


def _agent_worker_available() -> bool:
    """POST /start's worker check (worker_probe, #512), AGENT_WORKER_CHECK
    included. Fails closed: if the check itself cannot run, no run is
    created."""
    try:
        return bool(asyncio.run(worker_probe.agent_worker_available()))
    except Exception:
        logger.warning("Agent worker check failed", exc_info=True)
        return False


def _get_repository(sb, repository_id, installation_id, org_id) -> Optional[dict]:
    result = (
        sb.table("repositories")
        .select("id, full_name")
        .eq("id", repository_id)
        .eq("org_id", org_id)
        .eq("github_installation_id", installation_id)
        .limit(1)
        .maybe_single()
        .execute()
    )
    return getattr(result, "data", None) if result is not None else None


def _active_run(sb, repository_id, issue_number) -> Optional[dict]:
    rows = (
        sb.table("runs")
        .select("id, status")
        .eq("repository_id", repository_id)
        .eq("target_issue_number", issue_number)
        .in_("status", list(ACTIVE_RUN_STATUSES))
        .limit(1)
        .execute()
        .data
    )
    return rows[0] if rows else None


def _org_owner(sb, org_id) -> Optional[str]:
    rows = (
        sb.table("organization_members")
        .select("user_id")
        .eq("org_id", org_id)
        .eq("role", "owner")
        .order("created_at")
        .limit(1)
        .execute()
        .data
    )
    return rows[0]["user_id"] if rows else None


def _github_id(value) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _start_label_run(sb, event: dict) -> None:
    """issues.labeled: start a run when the trigger label was added."""
    label = trigger_label()
    if label is None:
        raise Ignored(f"{TRIGGER_LABEL_ENV} is not set")
    payload = event.get("payload") or {}
    added = (payload.get("label") or {}).get("name")
    if not isinstance(added, str) or added.casefold() != label.casefold():
        raise Ignored(f"label {added!r} is not the trigger label")

    issue = payload.get("issue") or {}
    number = issue.get("number")
    if not _github_id(number) or "pull_request" in issue:
        raise Ignored("the labeled item is not an issue")
    if issue.get("state") != "open":
        raise Ignored(f"issue #{number} is not open")

    sender = payload.get("sender") or {}
    sender_id = _github_id(sender.get("id"))
    sender_login = sender.get("login")
    sender_login = sender_login if isinstance(sender_login, str) else None
    if sender_id is None or sender.get("type") != "User":
        raise Ignored(f"the label was added by {sender_login!r}, not a GitHub user")
    author_id = _github_id((issue.get("user") or {}).get("id"))
    if (
        author_id == sender_id
        and issue.get("author_association") not in INSIDER_ASSOCIATIONS
    ):
        raise Ignored(
            f"the label on #{number} was applied for its author {sender_login!r}, "
            "who is not a repository insider (issue template?)"
        )

    installation_id = event["github_installation_id"]
    org_id = event["org_id"]
    repo_payload = payload.get("repository") or {}
    repository_id = repo_payload.get("id")
    repository = _get_repository(sb, repository_id, installation_id, org_id)
    if not repository:
        logger.info(
            "Trigger label on %s#%s ignored: repository %s is not registered "
            "to org %s with installation %s",
            repo_payload.get("full_name"),
            number,
            repository_id,
            org_id,
            installation_id,
        )
        raise Ignored(f"repository {repository_id} is not registered")
    repo_name = repository["full_name"]
    installation = _get_installation(sb, installation_id, org_id)
    if not installation or installation.get("suspended_at"):
        raise Ignored(f"installation {installation_id} is suspended or unknown")

    active = _active_run(sb, repository_id, number)
    if active:
        logger.info(
            "Trigger label on %s#%s ignored: run %s is %s",
            repo_name,
            number,
            active["id"],
            active["status"],
        )
        raise Ignored(f"run {active['id']} is already {active['status']}")

    if not _agent_worker_available():
        logger.warning(
            "Trigger label on %s#%s ignored: no agent worker is available",
            repo_name,
            number,
        )
        raise Ignored("no agent worker is available")

    user_id = _org_owner(sb, org_id)
    if not user_id:
        raise Ignored(f"org {org_id} has no owner to attribute the run to")

    run_id = _queue_run(
        sb,
        org_id=org_id,
        user_id=user_id,
        repository_id=repository_id,
        installation_id=installation_id,
        repo_name=repo_name,
        issue_number=number,
        sender_id=sender_id,
        sender_login=sender_login,
    )
    logger.info(
        "Run %s queued for %s#%s: label %r added by GitHub user %s (%s)",
        run_id,
        repo_name,
        number,
        added,
        sender_id,
        sender_login,
    )


def _queue_run(
    sb,
    *,
    org_id,
    user_id,
    repository_id,
    installation_id,
    repo_name,
    issue_number,
    sender_id,
    sender_login,
) -> str:
    """Create and enqueue the run as POST /start does; return its id."""
    from tasks import log_run_event, run_agent_loop_task

    run_id = str(uuid.uuid4())
    now = _now()
    try:
        sb.table("runs").insert(
            {
                "id": run_id,
                "org_id": org_id,
                "user_id": user_id,
                "repository_id": repository_id,
                "github_installation_id": installation_id,
                "repo_name": repo_name,
                "target_issue_number": issue_number,
                "mode": "autonomous",
                "status": "queued",
                "trigger_source": TRIGGER_SOURCE,
                "trigger_github_id": sender_id,
                "trigger_login": sender_login,
                "queued_at": now,
                "created_at": now,
                "updated_at": now,
            }
        ).execute()
    except Exception as exc:
        if not is_active_run_conflict(exc):
            raise
        # Another delivery queued a run for this issue after _active_run
        # looked: the index kept it to one run (#518).
        logger.info(
            "Trigger label on %s#%s ignored: another run for the issue was "
            "queued concurrently",
            repo_name,
            issue_number,
        )
        raise Ignored(f"a run for #{issue_number} is already active") from exc
    try:
        task = run_agent_loop_task.apply_async(
            args=[run_id, repo_name, org_id, user_id, repository_id, installation_id],
            kwargs={
                "target_issue_number": issue_number,
                "mode": "autonomous",
                "trigger_login": sender_login,
                "trigger_github_id": sender_id,
                "trigger_source": TRIGGER_SOURCE,
            },
            queue="agent_runs",
        )
    except Exception as exc:
        # As /start does: the row must not stay 'queued'. The event is then
        # retried (process_event), and a failed run blocks no new one.
        sb.table("runs").update(
            {
                "status": "failed",
                "error_message": f"Failed to queue task: {exc}",
                "completed_at": _now(),
            }
        ).eq("id", run_id).execute()
        raise
    sb.table("runs").update({"celery_task_id": task.id}).eq("id", run_id).execute()
    asyncio.run(
        log_run_event(
            run_id=run_id,
            org_id=org_id,
            agent_name="System",
            message=(
                f"Started by the trigger label on issue #{issue_number}, added by "
                f"GitHub user {sender_login or sender_id} (id {sender_id})"
            ),
        )
    )
    return run_id


def _handle(sb, event: dict) -> None:
    """Apply one event. Raises Ignored when there is nothing to do."""
    event_type = event.get("event_type")
    action = event.get("action")
    installation_id = event["github_installation_id"]
    org_id = event["org_id"]

    if event_type == "installation":
        if action in DEACTIVATING_ACTIONS:
            # GitHub's own timestamp when the payload carries one (suspend).
            payload_installation = (event.get("payload") or {}).get("installation")
            suspended_at = (payload_installation or {}).get("suspended_at") or _now()
            _set_suspended_at(sb, installation_id, org_id, suspended_at)
            # Tokens this process already holds must not outlive the
            # suspension; minting re-checks suspended_at (github_app).
            invalidate_installation_tokens(installation_id)
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

    if event_type == "issues" and action == "labeled":
        _start_label_run(sb, event)
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
