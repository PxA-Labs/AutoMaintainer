"""The label trigger's dedup is check-then-insert, not atomic (#518).

webhook_processor._start_label_run looks for an active run of the issue
(_active_run) and inserts a new one (_queue_run) in separate steps. With the
github_sync worker at concurrency above 1, two deliveries for the same issue
(the label removed and re-added quickly) can both pass the check before
either inserts. The fix #518 proposes is a partial unique index on
runs(repository_id, target_issue_number) for active statuses plus handling
the conflict; until then this test documents the race and fails strictly
(xfail strict=True), so the fix has to flip it.

The database is test_webhook_processor's in-memory FakeDB; the two
deliveries run in two threads, held after the check until both have done it.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import webhook_processor
from test_webhook_label_trigger import labeled, trigger, world  # noqa: F401
from webhook_processor import process_event


@pytest.mark.xfail(
    strict=True,
    reason="#518: _active_run and the run insert are not atomic; two concurrent "
    "label deliveries for one issue both start a run",
)
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
            both_checked.wait(2)
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
    assert "completed" in outcomes
    assert len(active) == 1
    assert trigger.task.apply_async.call_count == 1
