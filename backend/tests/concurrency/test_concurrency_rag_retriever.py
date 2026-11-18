"""An engine's injected retriever factory (#526): ``AutoMaintainerEngine(retriever=)``.

The factory is published on the engine's context and reaches the real PM
node of the engine's own runs; an engine built without one keeps the
default behaviour, and two engines running together never see each other's
retriever. The PM's LLM call is faked; nothing reaches a network.
"""

import asyncio

import pytest

import agents
from automaintainer._runtime.retrieval import RetrievalScope, RetrievedChunk
from automaintainer.context import active_engine
from automaintainer.engine import AutoMaintainerEngine

from concurrency_world import NOOP_NODES, REPO, REPOSITORY_ID, make_config

REJECT = (
    '{"verdict": "reject", "reasons": ["Not now."], '
    '"blocking_issues": ["Out of scope."]}'
)


class FakeRetriever:
    def __init__(self, name):
        self.name = name
        self.calls = []

    def retrieve(self, query, k=5):
        self.calls.append((query, k))
        return [RetrievedChunk(f"{self.name}.py", None, 1, 2, self.name, 0.9)]


def factory_for(name, scopes):
    retriever = FakeRetriever(name)

    def factory(scope):
        scopes.append(scope)
        return retriever

    factory.retriever = retriever
    return factory


@pytest.fixture
def pm_prompts(monkeypatch):
    """The PM's user prompts, by the engine whose run made them."""
    monkeypatch.setattr(agents, "gh", None)
    for name in ("RAG_RETRIEVAL_ENABLED", "RAG_TOP_K"):
        monkeypatch.delenv(name, raising=False)
    seen = []

    async def llm(system, user):
        ctx = active_engine()
        seen.append((ctx.engine_id if ctx else None, user))
        return REJECT

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    return seen


def nodes(barrier=None):
    """Every node a no-op except the real PM; the architect proposes the
    idea (and, with a barrier, waits until every run is in flight)."""

    async def architect(state):
        if barrier is not None:
            await asyncio.wait_for(barrier.wait(), 10)
        return {
            "idea": "Fix parse_int_list dropping negatives",
            "architect_directive": "parse_int_list drops negatives",
        }

    return {**NOOP_NODES, "architect": architect, "pm": agents.pm_node}


async def run_once(engine, run_id):
    return await engine.run(REPO, run_id=run_id, repository_id=REPOSITORY_ID)


async def test_an_engine_hands_its_retriever_to_the_pm(tmp_path, pm_prompts):
    scopes = []
    factory = factory_for("alpha", scopes)
    config = make_config(tmp_path, "alpha", rag_retrieval_enabled=True, rag_top_k=3)
    async with AutoMaintainerEngine(config, retriever=factory, nodes=nodes()) as engine:
        assert engine._context.retriever is factory
        result = await run_once(engine, "alpha-run")

    assert result.status == "completed"
    assert scopes == [RetrievalScope(repository_id=REPOSITORY_ID)]
    ((query, k),) = factory.retriever.calls
    assert "parse_int_list" in query and k == 3
    ((engine_id, user),) = pm_prompts
    assert engine_id == engine.engine_id
    assert agents.RAG_CONTEXT_HEADER in user and "alpha.py" in user


async def test_an_engine_without_a_retriever_gives_the_pm_no_context(
    tmp_path, pm_prompts
):
    config = make_config(tmp_path, "plain")
    async with AutoMaintainerEngine(config, nodes=nodes()) as engine:
        assert engine._context.retriever is None
        result = await run_once(engine, "plain-run")
    assert result.status == "completed"
    ((_, user),) = pm_prompts
    assert agents.RAG_CONTEXT_HEADER not in user


async def test_a_retriever_with_retrieval_off_is_never_called(tmp_path, pm_prompts):
    scopes = []
    config = make_config(tmp_path, "off")
    async with AutoMaintainerEngine(
        config, retriever=factory_for("off", scopes), nodes=nodes()
    ) as engine:
        await run_once(engine, "off-run")
    assert scopes == []
    assert agents.RAG_CONTEXT_HEADER not in pm_prompts[0][1]


async def test_two_engines_never_share_a_retriever(tmp_path, pm_prompts):
    barrier = asyncio.Barrier(4)
    scopes = {"alpha": [], "beta": []}
    engines = {
        name: AutoMaintainerEngine(
            make_config(tmp_path, name, rag_retrieval_enabled=True),
            retriever=factory_for(name, scopes[name]),
            nodes=nodes(barrier),
        )
        for name in ("alpha", "beta")
    }
    results = await asyncio.gather(
        *(
            run_once(engine, f"{name}-run{i}")
            for name, engine in engines.items()
            for i in range(2)
        )
    )
    for engine in engines.values():
        await engine.aclose()

    assert [r.status for r in results] == ["completed"] * 4
    assert len(pm_prompts) == 4
    for name, engine in engines.items():
        other = "beta" if name == "alpha" else "alpha"
        mine = [user for eid, user in pm_prompts if eid == engine.engine_id]
        assert len(mine) == 2
        assert all(f"{name}.py" in u and f"{other}.py" not in u for u in mine)
        assert scopes[name] == [RetrievalScope(repository_id=REPOSITORY_ID)] * 2
