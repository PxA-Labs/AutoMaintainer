"""Regression tests: StreamingEmitter's queue is bounded and never blocks emit()."""

import asyncio

import pytest

from events import DEFAULT_STREAM_QUEUE_MAXSIZE, ErrorEvent, StreamingEmitter


def _event(i):
    return ErrorEvent(run_id="r", error_code=f"E{i}", message=str(i))


def test_default_queue_is_bounded():
    assert StreamingEmitter().queue.maxsize == DEFAULT_STREAM_QUEUE_MAXSIZE > 0


@pytest.mark.asyncio
async def test_full_queue_drops_oldest_without_blocking():
    emitter = StreamingEmitter(maxsize=3)
    for i in range(5):
        # Without a consumer an unbounded/blocking put would hang or grow forever.
        await asyncio.wait_for(emitter.emit(_event(i)), timeout=1)

    assert emitter.queue.qsize() == 3
    assert emitter.dropped_count == 2
    codes = [emitter.queue.get_nowait().error_code for _ in range(3)]
    assert codes == ["E2", "E3", "E4"]


@pytest.mark.asyncio
async def test_drop_oldest_keeps_join_accounting_consistent():
    emitter = StreamingEmitter(maxsize=2)
    for i in range(4):
        await emitter.emit(_event(i))
    for _ in range(emitter.queue.qsize()):
        emitter.queue.get_nowait()
        emitter.queue.task_done()
    await asyncio.wait_for(emitter.queue.join(), timeout=1)
