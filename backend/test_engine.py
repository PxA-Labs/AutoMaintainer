"""AutoMaintainerEngine (#186): isolation, cancellation, ContextVars, parity.

The engine tests run graphs whose nodes call the runtime helpers the real
nodes use (get_supabase, the LLM through _llm_or_abort, the run's GitHub
token, the App slug, the workspace, merge policy, change limits,
verification), so each assertion checks that the helper resolved against the
engine running it. Nothing reaches GitHub, Supabase or Groq.
"""

import asyncio
import contextvars
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import agents
import change_limits
import github_app
import merge_policy
import verification
import workspace
from automaintainer import AutoMaintainerConfig, AutoMaintainerEngine, RunResult
from automaintainer import engine as engine_module
from automaintainer.config import ModelRouting
from automaintainer.context import active_engine, current_engine
from events import AgentPhase, BaseEvent, ErrorEvent, EventEmitter, TelemetryEvent
from test_agents_import_side_effects import BACKEND_DIR, PROBE

# --- Fakes ----------------------------------------------------------------------


class Collector(EventEmitter):
    def __init__(self):
        self.events = []

    async def emit(self, event: BaseEvent) -> None:
        self.events.append(event)


class FakeSupabase:
    """Records every write as (table, op, payload, filters)."""

    def __init__(self):
        self.writes = []

    def table(self, name):
        return _Query(self, name)


class _Query:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op, self.payload, self.filters = "select", None, []

    def _set(self, op, payload=None):
        self.op, self.payload = op, payload
        return self

    def insert(self, payload):
        return self._set("insert", payload)

    def upsert(self, payload, **_):
        return self._set("upsert", payload)

    def update(self, payload):
        return self._set("update", payload)

    def select(self, *_a, **_k):
        return self._set("select")

    def eq(self, column, value):
        self.filters.append((column, value))
        return self

    def limit(self, *_):
        return self

    def execute(self):
        if self.op != "select":
            self.db.writes.append((self.name, self.op, self.payload, self.filters))
        return SimpleNamespace(data=[])


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def __call__(self, *, system_prompt, user_prompt, model, api_key):
        self.calls.append((model, api_key))
        await asyncio.sleep(0.005)
        return f"answer from {model}"


class FakeCreds:
    mode = github_app.AUTH_MODE_APP

    def __init__(self, token):
        self._token = token

    def token(self):
        return self._token

    def client(self):  # pragma: no cover - the test nodes never call GitHub
        raise AssertionError("no GitHub calls in these tests")


def _run_of(write):
    """The run a recorded write belongs to."""
    _table, _op, payload, filters = write
    return payload.get("run_id") or payload.get("id") or dict(filters).get("id")


def make_config(tmp_path, name, **overrides):
    values = {
        "groq_api_keys": [f"{name}-key-1", f"{name}-key-2"],
        "workspace_dir": tmp_path / f"ws-{name}",
        **overrides,
    }
    return AutoMaintainerConfig(**values)


# Records what each helper resolved to, in whichever run executes it.
async def probe_node(state):
    run_id = agents.current_run_id.get()
    observed = {
        "engine": active_engine().engine_id if active_engine() else None,
        "supabase": id(agents.get_supabase()),
        "token": agents._run_github_token(),
        "slug": agents._github_app_slug(),
        "clone_dir": str(workspace.get_safe_repo_dir(state["repo_name"], 42)),
        "merge_policy": merge_policy.deployment_ceiling(),
        "max_files": change_limits.default_limits().max_files,
        "verify": verification.verification_enabled(),
        "keys": agents.get_all_groq_keys(),
    }
    await workspace.scrub_workspace_credentials()
    await asyncio.sleep(0.01)  # let the other engine's run interleave
    answer = await agents._llm_or_abort(AgentPhase.ARCHITECT, "sys", "user")
    await agents.emit_event(
        TelemetryEvent(
            run_id=run_id,
            phase=AgentPhase.ARCHITECT,
            latency_ms=1,
            model_name=str(answer),
        )
    )
    observed["llm_answer"] = str(answer)
    PROBES[run_id] = observed
    return {"log_messages": [{"agent": "Probe", "msg": f"probed {run_id}"}]}


async def noop_node(state):
    return {}


PROBES = {}
# Every node replaced: a default node would call real services.
NOOP_NODES = {name: noop_node for name in agents.GRAPH_NODE_NAMES}
PROBE_NODES = {**NOOP_NODES, "architect": probe_node}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    PROBES.clear()
    monkeypatch.setattr(workspace, "_scrubbed_roots", set())

    # GET /app answers with the slug of whichever App signs the request.
    def integration():
        app_id = github_app.current_app_identity().app_id
        return SimpleNamespace(get_app=lambda: SimpleNamespace(slug=f"app-{app_id}"))

    monkeypatch.setattr(github_app, "get_github_integration", integration)

    # Installation tokens are minted per App (no network).
    def mint(installation_id, repository, required, optional, app=None):
        app_id = (app or github_app.current_app_identity()).app_id
        return f"tok-{app_id}-{installation_id}", time.time() + 3600

    monkeypatch.setattr(github_app, "_mint_installation_token", mint)
    github_app._installation_token_cache.clear()
    yield
    github_app._installation_token_cache.clear()


# --- Isolation ------------------------------------------------------------------


async def test_two_engines_run_concurrently_without_cross_talk(tmp_path):
    db_a, db_b = FakeSupabase(), FakeSupabase()
    llm_a, llm_b = FakeLLM(), FakeLLM()
    events_a, events_b = Collector(), Collector()
    config_a = make_config(
        tmp_path,
        "a",
        github_app_id="101",
        github_private_key="pem-a",
        merge_policy="human",
        change_max_files=3,
        verify_changes=True,
        model_routing=ModelRouting(architect="model-a"),
    )
    config_b = make_config(
        tmp_path,
        "b",
        merge_policy="auto",
        change_max_files=7,
        verify_changes=False,
        github_auth_mode="app",
        github_app_id="202",
        github_private_key="pem-b",
        model_routing=ModelRouting(architect="model-b"),
    )
    # Engine B injects its credentials factory; engine A derives them from
    # its App id through github_app (installation tokens keyed by App).
    seen_factory_calls = []

    def github_b(installation_id, *, purpose, repository):
        seen_factory_calls.append((installation_id, repository))
        return FakeCreds(f"injected-{installation_id}")

    engine_a = AutoMaintainerEngine(
        config_a, supabase=db_a, llm=llm_a, emitter=events_a, nodes=PROBE_NODES
    )
    engine_b = AutoMaintainerEngine(
        config_b,
        supabase=db_b,
        llm=llm_b,
        github=github_b,
        emitter=events_b,
        nodes=PROBE_NODES,
    )
    async with engine_a, engine_b:
        result_a, result_b = await asyncio.gather(
            engine_a.run("o/r", run_id="run-a", installation_id=7),
            engine_b.run("o/r", run_id="run-b", installation_id=7),
        )

    assert result_a.status == result_b.status == "completed"
    a, b = PROBES["run-a"], PROBES["run-b"]
    assert a["engine"] == engine_a.engine_id and b["engine"] == engine_b.engine_id
    # Each engine's own database client, and every row written to it is its own.
    assert a["supabase"] == id(db_a) and b["supabase"] == id(db_b)
    assert {_run_of(w) for w in db_a.writes} == {"run-a"}
    assert {_run_of(w) for w in db_b.writes} == {"run-b"}
    assert ("runs", "upsert") in {(w[0], w[1]) for w in db_a.writes}
    assert {"status": "completed"} in [w[2] for w in db_b.writes]
    # Tokens: A minted for its App, B from its injected factory.
    assert a["token"] == "tok-101-7"
    assert b["token"] == "injected-7"
    assert seen_factory_calls == [(7, "o/r")]
    assert [k[3] for k in github_app._installation_token_cache] == ["101"]
    # App slug per engine.
    assert a["slug"] == "app-101" and b["slug"] == "app-202"
    assert engine_a._context.slug_cache == {"slug": "app-101"}
    assert engine_b._context.slug_cache == {"slug": "app-202"}
    assert agents._github_app_slug_cache == {}
    # Workspaces, and the once-per-root scrub of each.
    assert Path(a["clone_dir"]).is_relative_to(tmp_path / "ws-a")
    assert Path(b["clone_dir"]).is_relative_to(tmp_path / "ws-b")
    assert workspace._scrubbed_roots == {
        str(tmp_path / "ws-a"),
        str(tmp_path / "ws-b"),
    }
    # Settings.
    assert (a["merge_policy"], b["merge_policy"]) == ("human", "auto")
    assert (a["max_files"], b["max_files"]) == (3, 7)
    assert (a["verify"], b["verify"]) == (True, False)
    assert a["keys"] == ["a-key-1", "a-key-2"]
    # LLM: routed model and keys from the engine's own pool.
    assert [m for m, _ in llm_a.calls] == ["model-a"]
    assert [m for m, _ in llm_b.calls] == ["model-b"]
    assert {k for _, k in llm_a.calls} <= {"a-key-1", "a-key-2"}
    assert {k for _, k in llm_b.calls} <= {"b-key-1", "b-key-2"}
    assert engine_a._rate_limiter is None  # released by aclose
    # Events reached only the emitting engine's emitter.
    assert {e.run_id for e in events_a.events} == {"run-a"}
    assert {e.run_id for e in events_b.events} == {"run-b"}
    assert [e.model_name for e in events_a.events if isinstance(e, TelemetryEvent)] == [
        "answer from model-a"
    ]


async def test_engine_without_supabase_never_uses_the_process_client(
    tmp_path, monkeypatch
):
    process_db = FakeSupabase()
    monkeypatch.setattr(agents, "supabase", process_db)
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"), llm=FakeLLM(), nodes=PROBE_NODES
    )
    result = await engine.run("o/r", run_id="run-a")

    assert result.status == "completed"
    assert PROBES["run-a"]["supabase"] == id(None)
    assert process_db.writes == []


def test_constructor_does_no_io(tmp_path, monkeypatch):
    def refuse(*_a, **_k):  # pragma: no cover - fails the test if reached
        raise AssertionError("I/O in the constructor")

    monkeypatch.setattr(agents, "build_graph", refuse)
    monkeypatch.setattr(engine_module, "RateLimitManager", refuse)
    engine = AutoMaintainerEngine(make_config(tmp_path, "a"))

    assert engine._graph is None and engine._rate_limiter is None
    assert not (tmp_path / "ws-a").exists()
    with pytest.raises(TypeError):
        AutoMaintainerEngine({"groq_api_keys": ["k"]})


async def test_graph_is_built_once_per_engine(tmp_path):
    a = AutoMaintainerEngine(make_config(tmp_path, "a"), nodes=PROBE_NODES)
    b = AutoMaintainerEngine(make_config(tmp_path, "b"), nodes=PROBE_NODES)

    assert a.graph() is a.graph()
    assert a.graph() is not b.graph()
    assert a.graph() is not agents.get_default_graph()


async def test_rate_limiter_uses_the_config_pool_and_budgets(tmp_path):
    config = make_config(
        tmp_path, "a", groq_requests_per_minute=5, groq_tokens_per_minute=900
    )
    engine = AutoMaintainerEngine(config)
    token = current_engine.set(engine._context)
    try:
        manager = await agents.get_rate_limit_manager()
    finally:
        current_engine.reset(token)

    assert manager is engine.rate_limiter()
    assert [k.key for k in manager.keys] == ["a-key-1", "a-key-2"]
    assert [k.name for k in manager.keys] == ["primary", "key_1"]
    assert {k.requests_per_minute for k in manager.keys} == {5}
    assert {k.tokens_per_minute for k in manager.keys} == {900}


async def test_rate_limiter_uses_every_configured_key(tmp_path):
    """The environment pool stops at GROQ_API_KEY_19; a configured pool does
    not drop keys past the 20th."""
    keys = [f"k{i}" for i in range(25)]
    engine = AutoMaintainerEngine(make_config(tmp_path, "a", groq_api_keys=keys))
    manager = engine.rate_limiter()
    await manager.initialize()
    try:
        assert [k.key for k in manager.keys] == keys
        assert manager.keys[0].name == "primary"
        assert manager.keys[24].name == "key_24"
    finally:
        await engine.aclose()


def test_installation_tokens_are_cached_per_app(tmp_path):
    contexts = [
        AutoMaintainerEngine(
            make_config(tmp_path, name, github_app_id=app_id, github_private_key="pem")
        )._context
        for name, app_id in (("a", "101"), ("b", "202"))
    ]
    tokens = []
    for ctx in contexts:
        token = current_engine.set(ctx)
        try:
            creds = github_app.credentials_for_installation(
                5, purpose="test", repository="o/r"
            )
        finally:
            current_engine.reset(token)
        # Outside the context too (as in a worker thread): the credential
        # keeps the App it was created for.
        tokens.append(creds.token())

    assert tokens == ["tok-101-5", "tok-202-5"]
    assert sorted(k[3] for k in github_app._installation_token_cache) == [
        "101",
        "202",
    ]
    github_app.invalidate_installation_tokens(5)
    assert github_app._installation_token_cache == {}


# --- ContextVars ----------------------------------------------------------------

RUN_VARS = (
    current_engine,
    agents.current_run_id,
    agents.current_emitter,
    agents._run_github_credentials,
    agents._run_trigger_trust,
)


def _snapshot():
    return [var.get("<unset>") for var in RUN_VARS]


async def _raising_node(state):
    raise RuntimeError("boom")


async def _slow_node(state):
    await asyncio.sleep(30)
    return {}


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
async def test_run_context_vars_are_reset(tmp_path, outcome):
    nodes = dict(PROBE_NODES)
    if outcome == "failed":
        nodes["architect"] = _raising_node
    if outcome == "cancelled":
        nodes["architect"] = _slow_node
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"), llm=FakeLLM(), nodes=nodes
    )
    agents.current_run_id.set("outer-run")
    before = _snapshot()
    seen_inside = []

    async def spy(*a, **k):
        seen_inside.append(_snapshot())
        return await real(*a, **k)

    real = agents._run_pipeline
    agents._run_pipeline = spy
    try:
        run = asyncio.create_task(engine.run("o/r", run_id="run-1"))
        if outcome == "cancelled":
            await asyncio.sleep(0.05)
            assert engine.cancel("run-1", grace_seconds=0)
        result = await run
    finally:
        agents._run_pipeline = real

    assert result.status == outcome
    assert seen_inside[0][0] is engine._context  # bound during the run
    assert seen_inside[0][1] == "run-1"
    assert _snapshot() == before
    assert agents.current_run_id.get() == "outer-run"


async def test_run_twice_in_one_task_does_not_leak(tmp_path):
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"), llm=FakeLLM(), nodes=PROBE_NODES
    )
    before = _snapshot()
    await engine.run("o/r", run_id="run-1")
    await engine.run("o/r", run_id="run-2")

    assert set(PROBES) == {"run-1", "run-2"}
    assert _snapshot() == before


# --- Cancellation ---------------------------------------------------------------


async def test_cancel_stops_at_the_next_node_boundary(tmp_path):
    calls = []
    started = asyncio.Event()

    async def architect(state):
        calls.append("architect")
        started.set()
        await asyncio.sleep(0.1)  # in flight when cancel arrives; finishes
        calls.append("architect done")
        return {}

    async def brainstormer(state):  # pragma: no cover - must never run
        calls.append("brainstormer")
        return {}

    db, events = FakeSupabase(), Collector()
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"),
        supabase=db,
        emitter=events,
        nodes={**NOOP_NODES, "architect": architect, "brainstormer": brainstormer},
    )
    run = asyncio.create_task(engine.run("o/r", run_id="run-1"))
    await started.wait()
    t0 = time.monotonic()
    assert engine.cancel("run-1")
    result = await run
    elapsed = time.monotonic() - t0

    assert elapsed < 0.5
    assert calls == ["architect", "architect done"]
    assert result.status == "cancelled" and result.error_code == "RUN_CANCELLED"
    fatal = [e for e in events.events if isinstance(e, ErrorEvent) and e.fatal]
    assert [e.error_code for e in fatal] == ["RUN_CANCELLED"]
    assert db.writes[-1][:3] == ("runs", "update", {"status": "cancelled"})
    assert engine.cancel("run-1") is False  # no longer running


async def test_cancel_with_grace_interrupts_a_long_node(tmp_path):
    events = Collector()
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"),
        emitter=events,
        nodes={**NOOP_NODES, "architect": _slow_node},
    )
    run = asyncio.create_task(engine.run("o/r", run_id="run-1"))
    await asyncio.sleep(0.05)
    t0 = time.monotonic()
    assert engine.cancel("run-1", grace_seconds=0.1)
    result = await run

    assert time.monotonic() - t0 < 0.5
    assert result.status == "cancelled"
    assert [e.error_code for e in events.events if isinstance(e, ErrorEvent)] == [
        "RUN_CANCELLED"
    ]


async def test_cancelling_the_caller_still_raises(tmp_path):
    events = Collector()
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"),
        emitter=events,
        nodes={**NOOP_NODES, "architect": _slow_node},
    )
    run = asyncio.create_task(engine.run("o/r", run_id="run-1"))
    await asyncio.sleep(0.05)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run
    assert [e.error_code for e in events.events if isinstance(e, ErrorEvent)] == [
        "RUN_CANCELLED"
    ]
    assert engine._runs == {}


class SlowCreds(FakeCreds):
    """Credentials whose token takes a while (a slow token mint)."""

    def token(self):
        time.sleep(0.3)
        return self._token


def _never_runs_node(calls):
    async def node(state):  # pragma: no cover - must never run
        calls.append("node")
        return {}

    return node


@pytest.mark.parametrize("grace_seconds", [0, None])
async def test_cancel_during_credential_binding_ends_the_run_cancelled(
    tmp_path, grace_seconds
):
    """A cancel landing before the graph starts still leaves a terminal
    status and one RUN_CANCELLED: with a grace period the task is cancelled
    in the preamble (outside the pipeline's guarded section), without one the
    run stops before its first node."""
    calls = []
    db, events = FakeSupabase(), Collector()
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"),
        supabase=db,
        emitter=events,
        github=lambda *a, **k: SlowCreds("tok"),
        nodes={**NOOP_NODES, "architect": _never_runs_node(calls)},
    )
    run = asyncio.create_task(engine.run("o/r", run_id="run-1"))
    await asyncio.sleep(0.05)  # inside creds.token()
    assert engine.cancel("run-1", grace_seconds=grace_seconds)
    result = await run

    assert result.status == "cancelled" and result.error_code == "RUN_CANCELLED"
    assert calls == []
    assert [e.error_code for e in events.events if isinstance(e, ErrorEvent)] == [
        "RUN_CANCELLED"
    ]
    assert [w[:3] for w in db.writes if w[0] == "runs"] == [
        (
            "runs",
            "upsert",
            {
                "id": "run-1",
                "repo_name": "o/r",
                "target_issue_number": None,
                "status": "running",
            },
        ),
        ("runs", "update", {"status": "cancelled"}),
    ]
    assert engine._runs == {}


class _SlowUpsertSupabase(FakeSupabase):
    """The "running" upsert takes a while (a slow database round trip)."""

    def table(self, name):
        query = super().table(name)
        execute = query.execute

        def slow_execute():
            if query.op == "upsert":
                time.sleep(0.3)
            return execute()

        query.execute = slow_execute
        return query


async def test_cancel_during_the_runs_upsert_is_not_overwritten(tmp_path):
    """The upsert's thread outlives a task cancel; "cancelled" is written
    after it, so the row does not end "running"."""
    db, events = _SlowUpsertSupabase(), Collector()
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"), supabase=db, emitter=events, nodes=NOOP_NODES
    )
    run = asyncio.create_task(engine.run("o/r", run_id="run-1"))
    await asyncio.sleep(0.05)  # inside the upsert
    assert engine.cancel("run-1", grace_seconds=0)
    result = await run

    assert result.status == "cancelled" and result.error_code == "RUN_CANCELLED"
    assert [w[1] for w in db.writes if w[0] == "runs"] == ["upsert", "update"]
    assert db.writes[-1][:3] == ("runs", "update", {"status": "cancelled"})
    assert [e.error_code for e in events.events if isinstance(e, ErrorEvent)] == [
        "RUN_CANCELLED"
    ]


async def test_caller_cancel_during_credential_binding_marks_the_run(tmp_path):
    db, events = FakeSupabase(), Collector()
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"),
        supabase=db,
        emitter=events,
        github=lambda *a, **k: SlowCreds("tok"),
        nodes=NOOP_NODES,
    )
    run = asyncio.create_task(engine.run("o/r", run_id="run-1"))
    await asyncio.sleep(0.05)
    run.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run

    assert [e.error_code for e in events.events if isinstance(e, ErrorEvent)] == [
        "RUN_CANCELLED"
    ]
    assert db.writes[-1][:3] == ("runs", "update", {"status": "cancelled"})


async def test_aclose_ends_runs_cancelled_without_raising_into_callers(tmp_path):
    db, events = FakeSupabase(), Collector()
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"),
        supabase=db,
        emitter=events,
        nodes={**NOOP_NODES, "architect": _slow_node},
    )
    run = asyncio.create_task(engine.run("o/r", run_id="run-1"))
    await asyncio.sleep(0.05)
    await engine.aclose()
    result = await run

    assert result.status == "cancelled" and result.error_code == "RUN_CANCELLED"
    assert [e.error_code for e in events.events if isinstance(e, ErrorEvent)] == [
        "RUN_CANCELLED"
    ]
    assert db.writes[-1][:3] == ("runs", "update", {"status": "cancelled"})
    with pytest.raises(RuntimeError, match="closed"):
        await engine.run("o/r")


async def test_run_agent_loop_refuses_a_run_id_already_in_progress(monkeypatch):
    db = FakeSupabase()
    monkeypatch.setattr(agents, "supabase", db)
    monkeypatch.setattr(
        agents, "app", agents.build_graph({**NOOP_NODES, "architect": _slow_node})
    )
    first = asyncio.create_task(
        agents.run_agent_loop("o/r", run_id="run-1", emitter=Collector())
    )
    await asyncio.sleep(0.05)
    writes_before = list(db.writes)
    second_events = Collector()

    second = await agents.run_agent_loop("o/r", run_id="run-1", emitter=second_events)

    assert second["status"] == "failed"
    assert second["error_code"] == "RUN_ALREADY_ACTIVE"
    assert db.writes == writes_before  # the first run's row is untouched
    assert second_events.events == []
    assert engine_module.cancel_active_run("run-1", grace_seconds=0)
    assert (await first)["status"] == "cancelled"


class _AsyncCallableNode:
    def __init__(self, calls):
        self.calls = calls

    async def __call__(self, state):
        self.calls.append("callable")
        return {}


async def _partial_target(calls, state):
    calls.append("partial")
    return {}


async def test_injected_async_callables_are_awaited(tmp_path):
    """A functools.partial of an async function and an object with an async
    __call__ are async nodes too (agents._stoppable)."""
    import functools

    calls = []
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"),
        nodes={
            **NOOP_NODES,
            "architect": functools.partial(_partial_target, calls),
            "brainstormer": _AsyncCallableNode(calls),
        },
    )
    result = await engine.run("o/r", run_id="run-1")

    assert result.status == "completed"
    assert calls == ["partial", "callable"]


async def test_stoppable_keeps_the_node_signature_and_arguments():
    """LangGraph inspects a node's signature to decide what to inject; the
    cancel guard shows the node's own and passes everything through."""
    import inspect

    seen = []

    async def node(state, config):
        seen.append(config)
        return {"done": True}

    guarded = agents._stoppable(node)
    assert list(inspect.signature(guarded).parameters) == ["state", "config"]
    assert guarded.node is node
    assert not hasattr(guarded, "__wrapped__")
    assert await guarded({}, config={"x": 1}) == {"done": True}
    assert seen == [{"x": 1}]


async def test_node_asking_for_config_gets_it_through_the_guard(tmp_path):
    seen = []

    async def architect(state, config):
        seen.append("configurable" in config)
        return {}

    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"), nodes={**NOOP_NODES, "architect": architect}
    )
    result = await engine.run("o/r", run_id="run-1")

    assert result.status == "completed"
    assert seen == [True]


async def test_cancel_during_the_last_node_keeps_the_run_completed(tmp_path):
    """pm is the last node here (no approved verdict): a cancel arriving while
    it runs has no next node to stop, so the run's outcome stands."""
    db, events = FakeSupabase(), Collector()
    holder = {}

    async def pm(state):
        assert holder["engine"].cancel("run-1")
        await asyncio.sleep(0.05)
        return {}

    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"),
        supabase=db,
        emitter=events,
        nodes={**NOOP_NODES, "pm": pm},
    )
    holder["engine"] = engine
    result = await engine.run("o/r", run_id="run-1")

    assert result.status == "completed"
    assert not [e for e in events.events if isinstance(e, ErrorEvent)]
    assert db.writes[-1][:3] == ("runs", "update", {"status": "completed"})


async def test_cancel_active_run_reaches_the_default_engine(monkeypatch):
    """run_agent_loop runs are cancellable in-process (tasks.cancel_run_task)."""
    db = FakeSupabase()
    monkeypatch.setattr(agents, "supabase", db)
    monkeypatch.setattr(
        agents, "app", agents.build_graph({**NOOP_NODES, "architect": _slow_node})
    )
    run = asyncio.create_task(
        agents.run_agent_loop("o/r", run_id="run-1", emitter=Collector())
    )
    await asyncio.sleep(0.05)
    assert engine_module.cancel_active_run("run-1", grace_seconds=0)
    result = await run

    assert result["status"] == "cancelled"
    assert result["error_code"] == "RUN_CANCELLED"
    assert db.writes[-1][:3] == ("runs", "update", {"status": "cancelled"})
    assert engine_module.cancel_active_run("missing") is False


def test_cancel_run_task_stops_an_in_process_run(monkeypatch):
    import tasks

    calls = []
    monkeypatch.setattr(
        engine_module, "cancel_active_run", lambda run_id: calls.append(run_id)
    )

    class Runs:
        def table(self, _):
            return self

        def select(self, *_):
            return self

        def eq(self, *_):
            return self

        def single(self):
            return self

        def execute(self):
            return SimpleNamespace(
                data={"org_id": "o", "status": "running", "repository_id": None}
            )

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(tasks, "get_supabase", lambda: Runs())
    monkeypatch.setattr(tasks, "update_run_status", noop)
    monkeypatch.setattr(tasks, "log_run_event", noop)

    assert tasks.cancel_run_task.run("run-1") == {"success": True}
    assert calls == ["run-1"]


# --- run_agent_loop parity ------------------------------------------------------


async def _scripted_architect(state):
    return {
        "architect_directive": "fix app.py",
        "log_messages": [{"agent": "Architect", "msg": "directive ready"}],
    }


async def _scripted_brainstormer(state):
    return {
        "idea": "an idea",
        "issue_number": 12,
        "log_messages": [{"agent": "Brainstormer", "msg": "issue #12"}],
    }


async def _scripted_pm(state):
    return {
        "pm_decision": json.dumps({"verdict": "reject", "reasons": ["no"]}),
        "log_messages": [{"agent": "PM", "msg": "rejected"}],
    }


SCRIPTED = {
    "architect": _scripted_architect,
    "brainstormer": _scripted_brainstormer,
    "pm": _scripted_pm,
    "implementer": noop_node,
    "maintainer": noop_node,
}
# The DB writes run_agent_loop made for this scripted run before the engine.
EXPECTED_WRITES = [
    (
        "runs",
        "upsert",
        {
            "id": "r1",
            "repo_name": "https://github.com/o/r",
            "target_issue_number": None,
            "status": "running",
        },
        [],
    ),
    ("logs", "Starting loop for repo: o/r..."),
    ("logs", "directive ready"),
    ("logs", "issue #12"),
    ("logs", "rejected"),
    ("logs", "Agent loop complete."),
    ("runs", "update", {"status": "completed"}, [("id", "r1")]),
]


def _summarise(writes):
    return [
        (t, p["message"]) if t == "logs" else (t, op, p, f) for t, op, p, f in writes
    ]


async def test_run_agent_loop_parity_with_the_previous_loop(monkeypatch):
    db = FakeSupabase()
    monkeypatch.setattr(agents, "supabase", db)
    monkeypatch.setattr(agents, "app", agents.build_graph(SCRIPTED))
    events = Collector()

    result = await agents.run_agent_loop(
        "https://github.com/o/r", run_id="r1", emitter=events
    )

    assert result == {
        "status": "completed",
        "summary": "",
        "issue_number": 12,
        "pr_number": 0,
        "branch_name": "",
        "merge_status": "",
    }
    assert _summarise(db.writes) == EXPECTED_WRITES
    assert {w[2]["run_id"] for w in db.writes if w[0] == "logs"} == {"r1"}
    assert events.events == []


async def test_engine_run_writes_what_run_agent_loop_writes(tmp_path, monkeypatch):
    legacy_db, engine_db = FakeSupabase(), FakeSupabase()
    monkeypatch.setattr(agents, "supabase", legacy_db)
    monkeypatch.setattr(agents, "app", agents.build_graph(SCRIPTED))
    legacy = await agents.run_agent_loop(
        "https://github.com/o/r", run_id="r1", emitter=Collector()
    )
    engine = AutoMaintainerEngine(
        make_config(tmp_path, "a"), supabase=engine_db, nodes=SCRIPTED
    )
    result = await engine.run("https://github.com/o/r", run_id="r1")

    assert result.raw == legacy
    assert isinstance(result, RunResult) and result.status == "completed"
    assert _summarise(engine_db.writes) == _summarise(legacy_db.writes)


def test_run_result_pr_url():
    raw = {"status": "completed", "pr_number": 9}
    result = RunResult.from_loop(raw, "r1", "https://github.com/o/r")
    assert result.pr_url == "https://github.com/o/r/pull/9"
    assert RunResult.from_loop({"status": "failed"}, "r1", "o/r").pr_url is None


# --- Import side effects --------------------------------------------------------


def test_importing_the_engine_has_no_network_or_file_side_effects(tmp_path):
    probe = PROBE.replace(
        "import agents  # noqa: E402",
        "import automaintainer.engine  # noqa: E402\nimport agents  # noqa: E402",
    )
    assert probe != PROBE
    cwd, tmp = tmp_path / "cwd", tmp_path / "tmp"
    cwd.mkdir()
    tmp.mkdir()
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(BACKEND_DIR),
            "PYTHONDONTWRITEBYTECODE": "1",
            "TMP": str(tmp),
            "TEMP": str(tmp),
            "TMPDIR": str(tmp),
            "SUPABASE_URL": "https://example.supabase.co",
            "SUPABASE_SERVICE_KEY": "service-role-key",
            "GITHUB_AUTH_MODE": "pat",
        }
    )
    proc = subprocess.run(
        [sys.executable, "-B", "-c", probe],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout.strip().splitlines()[-1])

    assert report["events"] == []
    assert list(cwd.iterdir()) == [] and list(tmp.iterdir()) == []
    assert report["supabase_built"] is False
    assert report["graph_built"] is False


def test_engine_context_is_not_visible_outside_runs():
    assert active_engine() is None
    assert contextvars.copy_context().get(current_engine) is None


# --- Call-time settings without an engine fall back to the environment -----------


def _in_engine(config, fn):
    ctx = AutoMaintainerEngine(config)._context
    token = current_engine.set(ctx)
    try:
        return fn()
    finally:
        current_engine.reset(token)


def test_settings_resolve_from_the_engine_else_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MERGE_POLICY", "auto")
    monkeypatch.setenv("CHANGE_LIMIT_MAX_FILES", "4")
    monkeypatch.setenv("VERIFY_ALLOW_LOCAL", "true")
    monkeypatch.setenv("VERIFY_TIMEOUT_SECONDS", "11")
    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path / "env-ws"))
    monkeypatch.setenv("GIT_TIMEOUT_SECONDS", "300")
    monkeypatch.delenv("SANDBOX_BACKEND", raising=False)
    config = make_config(
        tmp_path,
        "a",
        merge_policy="ci",
        change_max_files=2,
        forbidden_paths=("secret/**",),
        timeout_seconds=40,
        max_timeout_seconds=50,
        git_timeout_seconds=12.5,
        max_iterations=1,
        implementer_max_file_chars=99,
        sandbox_backend="no_such_module:factory",
    )

    def settings():
        return (
            merge_policy.deployment_ceiling(),
            change_limits.default_limits().max_files,
            change_limits.default_limits().deny,
            verification.local_execution_refusal() is None,
            verification._config_timeout({}),
            str(workspace.get_base_workspace_dir()),
            workspace.git_timeout_seconds(),
            agents.should_iterate({"review": "", "iteration": 1}),
            agents._implementer_limits()[0],
        )

    engine = _in_engine(config, settings)
    env = settings()

    assert engine == (
        "ci",
        2,
        ("secret/**",),
        False,  # runner_type "sandbox": no local execution
        40,
        str(tmp_path / "ws-a"),
        12.5,
        agents.END,  # max_iterations=1
        99,
    )
    assert env[:2] == ("auto", 4)
    assert env[3:6] == (True, 11, str(tmp_path / "env-ws"))
    assert env[7] == "implementer"  # the built-in limit of 3 iterations
    with pytest.raises(verification.RunnerError, match="sandbox_backend"):
        _in_engine(config, verification._sandbox_backend_factory)
    assert verification._sandbox_backend_factory() is None  # SANDBOX_BACKEND unset


def test_engine_without_workspace_dir_ignores_the_environment(tmp_path, monkeypatch):
    """workspace_dir=None is the documented runtime default, <tmp>/automaintainer,
    not whatever AUTOMAINTAINER_WORKSPACE_DIR the host process has."""
    import tempfile

    monkeypatch.setenv("AUTOMAINTAINER_WORKSPACE_DIR", str(tmp_path / "env-ws"))
    config = make_config(tmp_path, "a", workspace_dir=None)

    assert _in_engine(config, workspace.get_base_workspace_dir) == Path(
        os.path.abspath(os.path.join(tempfile.gettempdir(), "automaintainer"))
    )
    assert workspace.get_base_workspace_dir() == Path(
        os.path.abspath(tmp_path / "env-ws")
    )


def test_github_auth_mode_and_pat_come_from_the_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_AUTH_MODE", "pat")
    monkeypatch.setenv("GITHUB_TOKEN", "env-pat")
    pat_engine = make_config(tmp_path, "a", github_token="engine-pat")
    app_engine = make_config(
        tmp_path, "b", github_app_id="303", github_private_key="pem"
    )

    def creds():
        return github_app.credentials_for_installation(9, purpose="t")

    assert _in_engine(pat_engine, creds).token() == "engine-pat"
    assert _in_engine(app_engine, github_app.get_auth_mode) == "app"
    assert _in_engine(app_engine, creds).token() == "tok-303-9"
    assert creds().token() == "env-pat"
