"""The run's ContextVars: visible to everything the run starts, reset after it.

Inside a run, a task the node spawns and an ``asyncio.to_thread`` worker see
the run's engine and run id (they copy the context). After the run (success,
failure, an exception out of the pipeline, a cancel through the engine or of
the caller) the caller, a fresh thread and a fresh task see none of it, also
while a task or thread started by the run is still alive.
"""

import asyncio
import contextvars
import threading

import pytest

import agents
from automaintainer.context import active_engine, current_engine

from automaintainer.engine import AutoMaintainerEngine
from concurrency_world import (
    NOOP_NODES,
    REPO,
    Collector,
    FakeLLM,
    FakeSupabase,
    make_config,
    snapshot,
    thread_sample,
)


def _seen() -> tuple:
    ctx = active_engine()
    return (ctx.engine_id if ctx else None, agents.current_run_id.get(None))


class Spawner:
    """A node that starts a child task and a worker thread, records what they
    see, and leaves both alive past the end of the run (released later)."""

    def __init__(self, outcome: str):
        self.outcome = outcome
        self.entered = asyncio.Event()
        self.release_child = asyncio.Event()
        self.release_thread = threading.Event()
        self.seen = {}
        self.child = None
        self.thread_done = None

    async def node(self, state):
        self.seen["node"] = _seen()
        self.seen["child_now"] = await asyncio.create_task(_spawned_sample())
        self.seen["thread_now"] = await asyncio.to_thread(_seen)

        async def lingering_child():
            await self.release_child.wait()
            return _seen()

        def lingering_thread():
            self.release_thread.wait(10)
            return _seen()

        # Not awaited by the run: they outlive it.
        self.child = asyncio.create_task(lingering_child())
        self.thread_done = asyncio.ensure_future(asyncio.to_thread(lingering_thread))
        self.entered.set()
        if self.outcome == "failed":
            raise RuntimeError("node failed")
        if self.outcome in ("cancelled", "caller-cancelled"):
            await asyncio.sleep(30)
        return {}


async def _spawned_sample():
    return _seen()


OUTCOMES = ["completed", "failed", "cancelled", "caller-cancelled"]


@pytest.mark.parametrize("outcome", OUTCOMES)
async def test_run_context_reaches_spawned_work_and_is_reset(tmp_path, outcome):
    spawner = Spawner(outcome)
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"),
        supabase=FakeSupabase(),
        llm=FakeLLM(),
        emitter=Collector(),
        nodes={**NOOP_NODES, "architect": spawner.node},
    )
    outer_token = agents.current_run_id.set("outer-run")
    try:
        before = snapshot()
        run = asyncio.create_task(engine.run(REPO, run_id="run-1"))
        await spawner.entered.wait()
        expected = (engine.engine_id, "run-1")
        assert spawner.seen == {
            "node": expected,
            "child_now": expected,
            "thread_now": expected,
        }

        if outcome == "cancelled":
            assert engine.cancel("run-1", grace_seconds=0)
        if outcome == "caller-cancelled":
            run.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run
        else:
            result = await run
            assert result.status == outcome

        # The caller, a new task and a new thread see none of the run...
        assert snapshot() == before
        assert agents.current_run_id.get() == "outer-run"
        assert await asyncio.create_task(_spawned_sample()) == (None, "outer-run")
        assert await asyncio.to_thread(_seen) == (None, "outer-run")
        assert contextvars.copy_context().get(current_engine) is None
        # ...while what the run spawned keeps the run's context to the end.
        spawner.release_child.set()
        spawner.release_thread.set()
        assert await spawner.child == expected
        assert await spawner.thread_done == expected
        assert snapshot() == before
        assert engine._runs == {}
    finally:
        spawner.release_thread.set()
        agents.current_run_id.reset(outer_token)


async def test_exception_out_of_the_pipeline_resets_context(tmp_path, monkeypatch):
    """An error the pipeline does not turn into a failed result (here raised
    by the pipeline itself) propagates out of run() with nothing left bound."""
    seen = []

    async def exploding_pipeline(*args, **kwargs):
        seen.append(_seen())
        seen.append(await asyncio.to_thread(thread_sample))
        raise ValueError("pipeline exploded")

    real_pipeline = agents._run_pipeline
    monkeypatch.setattr(agents, "_run_pipeline", exploding_pipeline)
    db = FakeSupabase()
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"), supabase=db, nodes=NOOP_NODES
    )
    before = snapshot()

    with pytest.raises(ValueError, match="exploded"):
        await engine.run(REPO, run_id="run-1")

    assert seen[0] == (engine.engine_id, "run-1")
    assert seen[1]["engine"] == engine.engine_id
    assert seen[1]["supabase"] is db
    assert snapshot() == before
    assert await asyncio.to_thread(_seen) == (None, None)
    assert engine._runs == {}
    # The run id is free again.
    monkeypatch.setattr(agents, "_run_pipeline", real_pipeline)
    assert (await engine.run(REPO, run_id="run-1")).status == "completed"


async def test_interleaved_runs_in_one_task_restore_each_others_state(tmp_path):
    """Sequential runs of two engines in one task: after each, the task's
    context is exactly what it was before it."""
    engines = [
        AutoMaintainerEngine(make_config(tmp_path, n), nodes=NOOP_NODES)
        for n in ("a", "b")
    ]
    before = snapshot()
    for i in range(3):
        for engine in engines:
            assert (await engine.run(REPO, run_id=f"run-{i}")).status == "completed"
            assert snapshot() == before
