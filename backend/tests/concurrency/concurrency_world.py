"""Fakes and probes for the concurrency suite.

The basic fakes (event collector, recording Supabase, LLM transport,
credentials, ``make_config``, the no-op node table) are test_engine's, imported
rather than copied so the two suites cannot drift apart. What is added here:

- ``RUN_VARS`` / ``snapshot()``: every ContextVar an engine run binds.
- ``sample()``: what each runtime helper resolves to in the current context,
  the same helpers the real graph nodes call.
- ``ProbeWorld``: a graph node that samples before and after a barrier every
  concurrent run must reach (so all runs are provably in flight together),
  in a task it spawns and in an ``asyncio.to_thread`` worker, then writes a DB
  row, calls the LLM and emits an event through the engine's helpers.
- ``EngineSpec`` / ``build_engines``: N engines whose configurations differ in
  every isolated setting.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional

import agents
import change_limits
import github_app
import merge_policy
import verification
import workspace
from automaintainer.config import ModelRouting
from automaintainer.context import active_engine, current_engine
from automaintainer.engine import AutoMaintainerEngine
from events import AgentPhase, ErrorEvent, TelemetryEvent
from test_engine import (  # noqa: F401  (re-exported for the suite)
    NOOP_NODES,
    Collector,
    FakeCreds,
    FakeLLM,
    FakeSupabase,
    _run_of,
    make_config,
)

REPO = "o/r"
REPOSITORY_ID = 42
INSTALLATION_ID = 7

RUN_VARS = (
    current_engine,
    agents.current_run_id,
    agents.current_emitter,
    agents._run_github_credentials,
    agents._run_trigger_trust,
    agents._run_should_stop,
)


def snapshot() -> list:
    return [var.get("<unset>") for var in RUN_VARS]


def cancelled_codes(events: Collector) -> list:
    return [e.error_code for e in events.events if isinstance(e, ErrorEvent)]


def run_statuses(db: FakeSupabase, run_id: str) -> list:
    """Every status written to ``runs`` for ``run_id``, in write order."""
    return [
        w[2]["status"]
        for w in db.writes
        if w[0] == "runs" and _run_of(w) == run_id and "status" in (w[2] or {})
    ]


def thread_sample() -> dict:
    """What a worker thread sees (no awaits: runs in asyncio.to_thread)."""
    ctx = active_engine()
    return {
        "engine": ctx.engine_id if ctx else None,
        "run_id": agents.current_run_id.get(None),
        "supabase": agents.get_supabase(),
        "app_id": github_app.current_app_identity().app_id,
        "workspace": str(workspace.get_base_workspace_dir()),
        "token": agents._run_github_token(),
    }


async def sample(repo: str = REPO) -> dict:
    """What every runtime helper resolves to in this context."""
    ctx = active_engine()
    return {
        "engine": ctx.engine_id if ctx else None,
        "run_id": agents.current_run_id.get(None),
        "supabase": agents.get_supabase(),
        "rate_limiter": await agents.get_rate_limit_manager(),
        "app_id": github_app.current_app_identity().app_id,
        "token": agents._run_github_token(),
        "slug": agents._github_app_slug(),
        "workspace": str(workspace.get_base_workspace_dir()),
        "clone_dir": str(workspace.get_safe_repo_dir(repo, REPOSITORY_ID)),
        "merge_ceiling": merge_policy.deployment_ceiling(),
        "max_files": change_limits.default_limits().max_files,
        "verify": verification.verification_enabled(),
        "keys": agents.get_all_groq_keys(),
    }


class ProbeWorld:
    """A probe node shared by ``parties`` concurrent runs."""

    def __init__(self, parties: int, timeout: float = 20.0):
        self.barrier = asyncio.Barrier(parties)
        self.timeout = timeout
        self.records: dict[str, dict] = {}

    async def node(self, state):
        run_id = agents.current_run_id.get()
        before = await sample(state["repo_name"])
        await workspace.scrub_workspace_credentials()
        # Every run is inside this node before any continues.
        await asyncio.wait_for(self.barrier.wait(), self.timeout)
        after = await sample(state["repo_name"])
        child = await asyncio.create_task(sample(state["repo_name"]))
        in_thread = await asyncio.to_thread(thread_sample)

        db = agents.get_supabase()
        engine_id = active_engine().engine_id
        await asyncio.to_thread(
            lambda: db.table("probe")
            .insert({"run_id": run_id, "engine": engine_id})
            .execute()
        )
        answer = await agents._llm_or_abort(AgentPhase.ARCHITECT, "sys", "user")
        await agents.emit_event(
            TelemetryEvent(
                run_id=run_id,
                phase=AgentPhase.ARCHITECT,
                latency_ms=1,
                model_name=str(answer),
            )
        )
        self.records[run_id] = {
            "before": before,
            "after": after,
            "child": child,
            "thread": in_thread,
            "answer": str(answer),
        }
        return {"log_messages": [{"agent": "Probe", "msg": f"probed {run_id}"}]}

    def nodes(self) -> dict:
        return {**NOOP_NODES, "architect": self.node}


@dataclass
class EngineSpec:
    """One engine of the suite and everything injected into it."""

    index: int
    engine: AutoMaintainerEngine
    db: FakeSupabase
    llm: FakeLLM
    events: Collector
    app_id: str
    merge_policy: str
    max_files: int
    verify: bool
    model: str
    keys: list
    workspace: str
    injected_github: bool
    factory_calls: list = field(default_factory=list)
    run_ids: list = field(default_factory=list)

    @property
    def expected_token(self) -> str:
        if self.injected_github:
            return f"inj-{self.index}-{INSTALLATION_ID}"
        return f"tok-{self.app_id}-{INSTALLATION_ID}"


MERGE_POLICIES = ("human", "ci", "auto")


def _credentials_factory(spec: EngineSpec):
    def github(installation_id, *, purpose, repository):
        spec.factory_calls.append((installation_id, repository))
        return FakeCreds(f"inj-{spec.index}-{installation_id}")

    return github


def build_engines(tmp_path, count: int, nodes: dict) -> list[EngineSpec]:
    """``count`` engines that differ in every isolated setting.

    Odd engines inject a credentials factory; even ones mint installation
    tokens for their own App id. All use installation id INSTALLATION_ID, so
    a token cache keyed without the App would hand one App's token to another.
    """
    specs = []
    for i in range(count):
        name = f"e{i}"
        app_id = str(1000 + i)
        injected = i % 2 == 1
        config = make_config(
            tmp_path,
            name,
            github_app_id=app_id,
            github_private_key=f"pem-{i}",
            github_auth_mode="app",
            merge_policy=MERGE_POLICIES[i % 3],
            change_max_files=10 + i,
            verify_changes=i % 2 == 0,
            model_routing=ModelRouting(architect=f"model-{i}"),
        )
        spec = EngineSpec(
            index=i,
            engine=None,  # set below
            db=FakeSupabase(),
            llm=FakeLLM(),
            events=Collector(),
            app_id=app_id,
            merge_policy=MERGE_POLICIES[i % 3],
            max_files=10 + i,
            verify=i % 2 == 0,
            model=f"model-{i}",
            keys=[f"{name}-key-1", f"{name}-key-2"],
            workspace=str(tmp_path / f"ws-{name}"),
            injected_github=injected,
        )
        spec.engine = AutoMaintainerEngine(
            config,
            supabase=spec.db,
            llm=spec.llm,
            github=_credentials_factory(spec) if injected else None,
            emitter=spec.events,
            nodes=nodes,
        )
        specs.append(spec)
    return specs


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.001):
    """Poll ``predicate`` on the loop until it is true (fails the test on timeout)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(interval)


def gated_node(entered: asyncio.Event, release: Optional[asyncio.Event] = None):
    """A node that signals it started and (optionally) waits to be released."""

    async def node(state):
        entered.set()
        if release is not None:
            await release.wait()
        return {}

    return node
