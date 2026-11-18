"""Two label deliveries for one issue at the same moment start one run (#518).

webhook_processor._start_label_run looks for an active run of the issue
(_active_run) and inserts a new one (_queue_run) in separate steps. With the
github_sync worker at concurrency above 1, two deliveries for the same issue
(the label removed and re-added quickly) can both pass the check before
either inserts. The database closes the race: the partial unique index
runs_one_active_per_issue rejects the second active run with a unique
violation (23505), which _queue_run records as 'ignored'.

The database is test_webhook_processor's in-memory FakeDB, which enforces
that index the way Postgres does (#524). The two deliveries run in two
threads, held after the check until both have done it, so both reach the
insert and the index decides.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import webhook_processor
import test_webhook_label_trigger as label_tests
from test_webhook_label_trigger import labeled, world
from webhook_processor import process_event

# The "trigger" fixture (feature on, worker available, Celery mocked).
trigger = label_tests.trigger


def test_concurrent_label_events_for_one_issue_start_one_run(trigger, monkeypatch):
    first = labeled(id="ev-1", github_delivery_id="d-1")
    second = labeled(id="ev-2", github_delivery_id="d-2")
    db = world(first, second)

    both_checked = threading.Barrier(2)
    real_active_run = webhook_processor._active_run

    def active_run_then_wait(sb, repository_id, issue_number):
        found = real_active_run(sb, repository_id, issue_number)
        try:
            # Both deliveries have looked before either inserts. A fix that
            # serialises the check (a lock) makes the barrier time out here,
            # which is fine: the second delivery then sees the first's run.
            both_checked.wait(15)
        except threading.BrokenBarrierError:
            pass
        return found

    monkeypatch.setattr(webhook_processor, "_active_run", active_run_then_wait)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(lambda ev: process_event(db, dict(ev)), [first, second])
        )

    active = [
        r
        for r in db["runs"]
        if r["target_issue_number"] == 12
        and r["status"] in webhook_processor.ACTIVE_RUN_STATUSES
    ]
    # One delivery queued the run; the index refused the other's insert.
    assert sorted(outcomes) == ["completed", "ignored"]
    assert len(active) == 1
    assert trigger.task.apply_async.call_count == 1
