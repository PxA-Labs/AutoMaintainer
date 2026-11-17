"""Cancellation racing the run: before it starts, around its last node, on
aclose with several runs in flight, and duplicate run ids.

The invariant checked in every race: the RunResult, the last status written
to the run's row and the RUN_CANCELLED events agree. A cancelled run has
exactly one RUN_CANCELLED and its row ends ``cancelled``; a completed run has
none and its row ends ``completed``.
"""

import asyncio
import threading
import time

import pytest

import agents
from automaintainer import engine as engine_module
from automaintainer.engine import AutoMaintainerEngine, RunAlreadyActive
from events import ErrorEvent

from concurrency_world import (
    NOOP_NODES,
    REPO,
    Collector,
    FakeCreds,
    FakeSupabase,
    cancelled_codes,
    gated_node,
    make_config,
    run_statuses,
    wait_until,
)


def _assert_consistent(result, db, events, run_id="run-1"):
    statuses = run_statuses(db, run_id)
    assert statuses[0] == "running"
    assert statuses[-1] == result.status, statuses
    expected_cancels = ["RUN_CANCELLED"] if result.status == "cancelled" else []
    assert cancelled_codes(events) == expected_cancels


def _engine(tmp_path, nodes, *, db=None, events=None, name="a", **kwargs):
    return AutoMaintainerEngine(
        make_config(tmp_path, name),
        supabase=db if db is not None else FakeSupabase(),
        emitter=events if events is not None else Collector(),
        nodes={**NOOP_NODES, **nodes},
        **kwargs,
    )


def _never(calls, name):
    async def node(state):  # pragma: no cover - must never run
        calls.append(name)
        return {}

    return node


# --- Cancel before the graph starts ---------------------------------------------


@pytest.mark.parametrize("grace_seconds", [None, 0])
async def test_cancel_as_soon_as_the_run_is_registered(tmp_path, grace_seconds):
    calls = []
    db, events = FakeSupabase(), Collector()
    engine = _engine(
        tmp_path, {"architect": _never(calls, "architect")}, db=db, events=events
    )
    run = asyncio.create_task(engine.run(REPO, run_id="run-1"))
    await wait_until(lambda: "run-1" in engine._runs)
    assert engine.cancel("run-1", grace_seconds=grace_seconds)
    result = await run

    assert result.status == "cancelled" and result.error_code == "RUN_CANCELLED"
    assert calls == []
    _assert_consistent(result, db, events)
    assert engine._runs == {}


@pytest.mark.parametrize("how", ["aclose", "caller-cancel"])
async def test_stop_before_the_run_task_takes_its_first_step(tmp_path, how):
    """aclose() (or cancelling the caller) right after run() registered the
    run, before the engine's task ever ran: the task is cancelled before its
    coroutine starts, so nothing inside it can mark the run."""
    calls = []
    db, events = FakeSupabase(), Collector()
    engine = _engine(
        tmp_path, {"architect": _never(calls, "architect")}, db=db, events=events
    )
    run = asyncio.create_task(engine.run(REPO, run_id="run-1"))
    # One loop step: run() registers the run and creates its task, which is
    # scheduled after this coroutine resumes, so it has not started yet.
    await asyncio.sleep(0)
    assert engine._runs["run-1"].task is not None

    if how == "aclose":
        await engine.aclose()
        result = await run
        assert result.status == "cancelled" and result.error_code == "RUN_CANCELLED"
    else:
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    assert calls == []
    assert cancelled_codes(events) == ["RUN_CANCELLED"]
    # No "running" row was ever written; the cancel marker's update is the
    # only write, so a row created earlier (queued) cannot stay active.
    assert run_statuses(db, "run-1") == ["cancelled"]
    assert engine._runs == {}


# --- Cancel racing completion ---------------------------------------------------

# With no-op nodes and no approved verdict the graph is architect ->
# brainstormer -> pm, so pm is the last node.


async def test_cancel_just_before_the_last_node_stops_it(tmp_path):
    calls = []
    holder = {}

    async def brainstormer(state):
        assert holder["engine"].cancel("run-1")
        return {}

    db, events = FakeSupabase(), Collector()
    engine = _engine(
        tmp_path,
        {"brainstormer": brainstormer, "pm": _never(calls, "pm")},
        db=db,
        events=events,
    )
    holder["engine"] = engine
    result = await engine.run(REPO, run_id="run-1")

    assert result.status == "cancelled"
    assert calls == []
    _assert_consistent(result, db, events)


@pytest.mark.parametrize(
    "grace_seconds, status", [(None, "completed"), (0, "cancelled")]
)
async def test_cancel_during_the_last_node(tmp_path, grace_seconds, status):
    """Without a grace period the node in flight finishes and, being the
    last, the run completes; with one the node is interrupted."""
    entered = asyncio.Event()
    release = asyncio.Event()
    db, events = FakeSupabase(), Collector()
    engine = _engine(
        tmp_path, {"pm": gated_node(entered, release)}, db=db, events=events
    )
    run = asyncio.create_task(engine.run(REPO, run_id="run-1"))
    await entered.wait()
    assert engine.cancel("run-1", grace_seconds=grace_seconds)
    await asyncio.sleep(0.01)
    release.set()
    result = await run

    assert result.status == status
    _assert_consistent(result, db, events)


class _CancelOnFinalLog(FakeSupabase):
    """Calls ``on_final_log`` from the worker thread that writes the
    "Agent loop complete." log: the graph has finished, the run has not."""

    def __init__(self, on_final_log):
        super().__init__()
        self.on_final_log = on_final_log

    def table(self, name):
        query = super().table(name)
        execute = query.execute

        def hooked():
            payload = query.payload or {}
            if name == "logs" and payload.get("message") == "Agent loop complete.":
                self.on_final_log()
            return execute()

        query.execute = hooked
        return query


@pytest.mark.parametrize(
    "grace_seconds, status", [(None, "completed"), (0, "cancelled")]
)
async def test_cancel_just_after_the_last_node(tmp_path, grace_seconds, status):
    """The cancel arrives from another thread after the last node returned.
    Without a grace period there is no node left to stop; with one the
    run's task is interrupted before it writes ``completed``."""
    events = Collector()
    holder = {}
    db = _CancelOnFinalLog(
        lambda: holder["engine"].cancel("run-1", grace_seconds=grace_seconds)
    )
    engine = _engine(tmp_path, {}, db=db, events=events)
    holder["engine"] = engine
    result = await engine.run(REPO, run_id="run-1")

    assert result.status == status
    _assert_consistent(result, db, events)


class _SlowCompletedWrite(FakeSupabase):
    """The final ``completed`` update is slow (a slow database round trip)
    and announces that it started."""

    def __init__(self):
        super().__init__()
        self.completed_write_started = threading.Event()

    def table(self, name):
        query = super().table(name)
        execute = query.execute

        def slow():
            if query.op == "update" and query.payload == {"status": "completed"}:
                self.completed_write_started.set()
                time.sleep(0.3)
            return execute()

        query.execute = slow
        return query


@pytest.mark.xfail(
    strict=True,
    reason=(
        "agents._run_pipeline does not wait for an in-flight 'completed' "
        "write before marking the run cancelled (unlike the 'running' upsert), "
        "so the slower write lands last and the row ends 'completed' while "
        "run() returns 'cancelled' with RUN_CANCELLED"
    ),
)
async def test_cancel_during_the_completed_write_keeps_row_and_result_in_step(
    tmp_path,
):
    db, events = _SlowCompletedWrite(), Collector()
    engine = _engine(tmp_path, {}, db=db, events=events)
    run = asyncio.create_task(engine.run(REPO, run_id="run-1"))
    await wait_until(db.completed_write_started.is_set)
    assert engine.cancel("run-1", grace_seconds=0)
    result = await run
    await asyncio.sleep(0.4)  # let the slow write's thread finish

    _assert_consistent(result, db, events)


# --- aclose with runs in flight ---------------------------------------------------


class _SlowUpsertFor(FakeSupabase):
    """The "running" upsert of ``run_id`` is slow."""

    def __init__(self, run_id):
        super().__init__()
        self.slow_run = run_id

    def table(self, name):
        query = super().table(name)
        execute = query.execute

        def slow():
            if (
                query.op == "upsert"
                and (query.payload or {}).get("id") == self.slow_run
            ):
                time.sleep(0.3)
            return execute()

        query.execute = slow
        return query


class _SlowToken(FakeCreds):
    def token(self):
        time.sleep(0.3)
        return self._token


async def test_aclose_stops_every_run_in_flight_and_no_other_engine(tmp_path):
    """Three runs of engine A at different stages (in the row upsert, in
    credential binding, inside a node) and one run of engine B: aclose(A)
    ends A's runs cancelled, each with one RUN_CANCELLED and a cancelled row,
    and leaves B's run to complete."""
    in_node = asyncio.Event()

    async def slow_architect(state):
        if agents.current_run_id.get() == "a-node":
            in_node.set()
            await asyncio.sleep(30)
        return {}

    def github(installation_id, *, purpose, repository):
        creds = _SlowToken if installation_id == 2 else FakeCreds
        return creds(f"tok-{installation_id}")

    db_a, events_a = _SlowUpsertFor("a-upsert"), Collector()
    engine_a = _engine(
        tmp_path,
        {"architect": slow_architect},
        db=db_a,
        events=events_a,
        github=github,
    )
    b_entered, b_release = asyncio.Event(), asyncio.Event()
    db_b, events_b = FakeSupabase(), Collector()
    engine_b = _engine(
        tmp_path,
        {"architect": gated_node(b_entered, b_release)},
        db=db_b,
        events=events_b,
        name="b",
    )

    runs_a = {
        run_id: asyncio.create_task(
            engine_a.run(REPO, run_id=run_id, installation_id=installation)
        )
        for run_id, installation in (("a-upsert", 1), ("a-creds", 2), ("a-node", 1))
    }
    run_b = asyncio.create_task(engine_b.run(REPO, run_id="b-1"))
    await asyncio.wait_for(asyncio.gather(in_node.wait(), b_entered.wait()), 5)
    await asyncio.sleep(0.05)  # a-upsert in its upsert, a-creds in token()

    await engine_a.aclose()
    results_a = {run_id: await task for run_id, task in runs_a.items()}
    b_release.set()
    result_b = await run_b

    for run_id, result in results_a.items():
        assert result.status == "cancelled", run_id
        assert result.error_code == "RUN_CANCELLED"
        assert run_statuses(db_a, run_id)[-1] == "cancelled", run_id
        cancels = [
            e
            for e in events_a.events
            if isinstance(e, ErrorEvent) and e.run_id == run_id
        ]
        assert [e.error_code for e in cancels] == ["RUN_CANCELLED"], run_id
    assert engine_a._runs == {} and engine_a._rate_limiter is None
    with pytest.raises(RuntimeError, match="closed"):
        await engine_a.run(REPO)

    assert result_b.status == "completed"
    assert cancelled_codes(events_b) == []
    assert run_statuses(db_b, "b-1") == ["running", "completed"]
    assert {e.run_id for e in events_a.events} == set(runs_a)
    assert {e.run_id for e in events_b.events} <= {"b-1"}


# --- Duplicate run ids ------------------------------------------------------------


async def test_duplicate_run_id_started_in_the_same_tick(tmp_path):
    db, events = FakeSupabase(), Collector()
    engine = _engine(tmp_path, {}, db=db, events=events)

    first, second = await asyncio.gather(
        engine.run(REPO, run_id="run-1"),
        engine.run(REPO, run_id="run-1"),
        return_exceptions=True,
    )

    assert first.status == "completed"
    assert isinstance(second, RunAlreadyActive)
    assert run_statuses(db, "run-1") == ["running", "completed"]
    assert engine._runs == {}


async def test_duplicate_run_id_leaves_the_active_run_alone(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    db, events = FakeSupabase(), Collector()
    engine = _engine(
        tmp_path, {"architect": gated_node(entered, release)}, db=db, events=events
    )
    first = asyncio.create_task(engine.run(REPO, run_id="run-1"))
    await entered.wait()
    writes_before = list(db.writes)
    events_before = list(events.events)
    active = engine._runs["run-1"]

    with pytest.raises(RunAlreadyActive):
        await engine.run(REPO, run_id="run-1")

    assert db.writes == writes_before and events.events == events_before
    assert engine._runs["run-1"] is active
    release.set()
    assert (await first).status == "completed"
    # Free again once the first run ended.
    assert (await engine.run(REPO, run_id="run-1")).status == "completed"


async def test_run_agent_loop_duplicates_started_together(monkeypatch):
    db = FakeSupabase()
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(agents, "supabase", db)
    monkeypatch.setattr(
        agents,
        "app",
        agents.build_graph({**NOOP_NODES, "architect": gated_node(entered, release)}),
    )

    async def release_when_entered():
        await entered.wait()
        release.set()

    results = await asyncio.gather(
        agents.run_agent_loop(REPO, run_id="run-1", emitter=Collector()),
        agents.run_agent_loop(REPO, run_id="run-1", emitter=Collector()),
        agents.run_agent_loop(REPO, run_id="run-1", emitter=Collector()),
        release_when_entered(),
    )

    statuses = sorted(r.get("error_code") or r["status"] for r in results[:3])
    assert statuses == ["RUN_ALREADY_ACTIVE", "RUN_ALREADY_ACTIVE", "completed"]
    assert run_statuses(db, "run-1") == ["running", "completed"]
    assert engine_module.cancel_active_run("run-1") is False
