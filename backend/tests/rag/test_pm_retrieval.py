"""Retrieved code in the PM prompt (#526), behind RAG_RETRIEVAL_ENABLED.

With the flag off the prompt is byte-for-byte what it was before retrieval
existed (pinned below). With it on, the PM gets the top k chunks for the
issue in a separate section, formatted as untrusted data and capped in
size; any retriever failure or timeout leaves the prompt as if it were off.
The retriever is resolved per run: an engine's injected factory, else one
built from the run's database client. Nothing here reaches a network.
"""

import logging
import time

import pytest

import agents
from automaintainer import AutoMaintainerConfig
from automaintainer._runtime.retrieval import (
    RetrievalScope,
    RetrievedChunk,
    format_untrusted_chunks,
)
from automaintainer.context import EngineContext, current_engine

REPO_ID = 4242
DIRECTIVE = "Targeted Issue #3: parse_int_list drops negatives\nSee ints.py."
IDEA = DIRECTIVE
REJECT = (
    '{"verdict": "reject", "reasons": ["Not now."], '
    '"blocking_issues": ["Out of scope."]}'
)
# The PM's user prompt before retrieval existed, for DIRECTIVE and IDEA.
PINNED_PROMPT = (
    "Directive:\n"
    '<untrusted-data source="directive">\n'
    "Targeted Issue #3: parse_int_list drops negatives\nSee ints.py.\n"
    "</untrusted-data>\n\n"
    "Review this idea:\n"
    '<untrusted-data source="proposed idea">\n'
    "Targeted Issue #3: parse_int_list drops negatives\nSee ints.py.\n"
    "</untrusted-data>"
)
CHUNKS = [
    RetrievedChunk(
        "ints.py", "parse_int_list", 1, 9, "def parse_int_list(s): ...", 0.9
    ),
    RetrievedChunk("cli.py", None, 1, 3, "import ints", 0.4),
]


class FakeRetriever:
    def __init__(self, chunks=CHUNKS, error=None, delay=0.0):
        self.chunks, self.error, self.delay = chunks, error, delay
        self.calls = []

    def retrieve(self, query, k=5):
        self.calls.append((query, k))
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return list(self.chunks[:k])


@pytest.fixture
def prompts(monkeypatch):
    monkeypatch.setattr(agents, "gh", None)
    seen = []

    async def llm(system, user):
        seen.append((system, user))
        return REJECT

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    for name in ("RAG_RETRIEVAL_ENABLED", "RAG_TOP_K"):
        monkeypatch.delenv(name, raising=False)
    return seen


@pytest.fixture
def default_factory(monkeypatch):
    """The process path's retriever: what it was asked for and what it gives."""
    state = {"retriever": FakeRetriever(), "scopes": []}

    def factory(scope):
        state["scopes"].append(scope)
        return state["retriever"]

    monkeypatch.setattr(agents, "_default_retriever", factory)
    return state


def pm_state(**overrides):
    state = {
        "repo_name": "o/r",
        "repository_id": REPO_ID,
        "idea": IDEA,
        "architect_directive": DIRECTIVE,
        "issue_number": 3,
        "target_issue": 3,
    }
    state.update(overrides)
    return state


def with_context(chunks):
    return (
        PINNED_PROMPT
        + "\n\n"
        + agents.RAG_CONTEXT_HEADER
        + "\n"
        + format_untrusted_chunks(chunks, max_total_chars=agents.RAG_CONTEXT_MAX_CHARS)
    )


async def test_flag_off_prompt_is_exactly_the_pre_retrieval_prompt(
    prompts, default_factory
):
    await agents.pm_node(pm_state())
    assert prompts[0][1] == PINNED_PROMPT
    assert default_factory["scopes"] == []  # the retriever is never built


@pytest.mark.parametrize("raw", ["0", "false", "nonsense"])
async def test_flag_off_spellings_leave_the_prompt_alone(
    monkeypatch, prompts, default_factory, raw
):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", raw)
    await agents.pm_node(pm_state())
    assert prompts[0][1] == PINNED_PROMPT
    assert default_factory["scopes"] == []


async def test_flag_on_adds_the_top_k_chunks_as_untrusted_data(
    monkeypatch, prompts, default_factory
):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "true")
    await agents.pm_node(pm_state())

    system, user = prompts[0]
    assert user == with_context(CHUNKS)
    # The system prompt is unchanged: the context carries its own notice.
    assert agents.UNTRUSTED_DATA_NOTICE in system
    # Scoped to the run's repository, any indexed commit; queried with the
    # issue text and the default k.
    assert default_factory["scopes"] == [RetrievalScope(repository_id=REPO_ID)]
    ((query, k),) = default_factory["retriever"].calls
    assert "parse_int_list drops negatives" in query and "See ints.py." in query
    assert k == 8


async def test_rag_top_k_is_honoured(monkeypatch, prompts, default_factory):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")
    monkeypatch.setenv("RAG_TOP_K", "1")
    await agents.pm_node(pm_state())
    assert default_factory["retriever"].calls[0][1] == 1
    assert prompts[0][1] == with_context(CHUNKS[:1])


async def test_context_is_capped_in_total(monkeypatch, prompts, default_factory):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")
    big = [RetrievedChunk(f"f{i}.py", None, 1, 150, "y" * 5000, 1.0) for i in range(8)]
    default_factory["retriever"] = FakeRetriever(chunks=big)
    await agents.pm_node(pm_state())
    user = prompts[0][1]
    context = user[len(PINNED_PROMPT) :]
    assert user.startswith(PINNED_PROMPT)
    assert len(context) <= agents.RAG_CONTEXT_MAX_CHARS + len(
        "\n\n" + agents.RAG_CONTEXT_HEADER + "\n"
    )
    assert "f0.py" in context and "f7.py" not in context


async def test_the_query_is_capped(monkeypatch, prompts, default_factory):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")
    await agents.pm_node(pm_state(idea="z" * 50_000))
    query = default_factory["retriever"].calls[0][0]
    assert len(query) <= agents.RAG_QUERY_MAX_CHARS


@pytest.mark.parametrize(
    "error", [RuntimeError("db down"), ImportError("google-genai"), ValueError("x")]
)
async def test_a_retriever_error_logs_and_continues_without_context(
    monkeypatch, prompts, default_factory, caplog, error
):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")
    default_factory["retriever"] = FakeRetriever(error=error)
    with caplog.at_level(logging.WARNING):
        result = await agents.pm_node(pm_state())
    assert prompts[0][1] == PINNED_PROMPT
    assert result["pm_decision"]  # the PM still decided
    assert type(error).__name__ in caplog.text
    assert "db down" not in caplog.text  # the message itself is not logged


async def test_building_the_retriever_can_fail_too(monkeypatch, prompts, caplog):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")

    def broken(scope):
        raise ConnectionError("supabase unreachable")

    monkeypatch.setattr(agents, "_default_retriever", broken)
    with caplog.at_level(logging.WARNING):
        await agents.pm_node(pm_state())
    assert prompts[0][1] == PINNED_PROMPT
    assert "ConnectionError" in caplog.text


async def test_a_slow_retriever_times_out_and_the_pm_continues(
    monkeypatch, prompts, default_factory, caplog
):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")
    monkeypatch.setattr(agents, "RAG_RETRIEVAL_TIMEOUT_SECONDS", 0.05)
    default_factory["retriever"] = FakeRetriever(delay=0.5)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING):
        await agents.pm_node(pm_state())
    assert time.monotonic() - started < 0.45
    assert prompts[0][1] == PINNED_PROMPT
    assert "TimeoutError" in caplog.text


async def test_no_retriever_or_no_chunks_means_no_section(
    monkeypatch, prompts, default_factory
):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")
    default_factory["retriever"] = None
    await agents.pm_node(pm_state())
    default_factory["retriever"] = FakeRetriever(chunks=[])
    await agents.pm_node(pm_state())
    assert [user for _, user in prompts] == [PINNED_PROMPT, PINNED_PROMPT]


async def test_a_run_without_a_repository_id_does_not_retrieve(
    monkeypatch, prompts, default_factory
):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")
    await agents.pm_node(pm_state(repository_id=None))
    assert prompts[0][1] == PINNED_PROMPT
    assert default_factory["scopes"] == []


async def test_a_decided_checkpoint_is_not_retrieved_again(
    monkeypatch, prompts, default_factory
):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")

    async def checkpoint(run_id):
        return {"pm_decision": REJECT}

    monkeypatch.setattr(agents, "_load_checkpoint", checkpoint)
    await agents.pm_node(pm_state())
    assert prompts == [] and default_factory["scopes"] == []


# --- The engine path -------------------------------------------------------------


def engine_context(retriever=None, **config):
    ctx = EngineContext(
        config=AutoMaintainerConfig(groq_api_keys=["gsk_test"], **config),
        engine_id="engine-rag-test",
        rate_limiter=lambda: None,
    )
    # The engine's injected retriever factory (see docs/RAG.md); the engine
    # constructor's ``retriever=`` argument sets this attribute.
    ctx.retriever = retriever
    return ctx


@pytest.fixture
def engine():
    """Publishes an engine context in the test's own task (each async test
    runs in a task with a copy of the context, so nothing leaks out)."""

    def bind(ctx):
        current_engine.set(ctx)
        return ctx

    return bind


async def test_an_engine_uses_its_injected_retriever_and_its_own_settings(
    monkeypatch, prompts, default_factory, engine
):
    # The process variables do not apply to an engine's run.
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "0")
    monkeypatch.setenv("RAG_TOP_K", "50")
    injected = FakeRetriever()
    scopes = []

    def factory(scope):
        scopes.append(scope)
        return injected

    engine(engine_context(factory, rag_retrieval_enabled=True, rag_top_k=1))
    await agents.pm_node(pm_state())
    assert scopes == [RetrievalScope(repository_id=REPO_ID)]
    assert injected.calls[0][1] == 1
    assert prompts[0][1] == with_context(CHUNKS[:1])
    assert default_factory["scopes"] == []


async def test_an_engine_with_retrieval_off_never_builds_a_retriever(
    monkeypatch, prompts, engine
):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")
    called = []
    engine(engine_context(lambda scope: called.append(scope)))
    await agents.pm_node(pm_state())
    assert prompts[0][1] == PINNED_PROMPT and called == []


async def test_an_engine_without_an_injected_retriever_and_database_gets_none(
    prompts, engine
):
    """No module-level client: an engine without supabase has no index."""
    engine(engine_context(None, rag_retrieval_enabled=True))
    await agents.pm_node(pm_state())
    assert prompts[0][1] == PINNED_PROMPT


async def test_two_engines_never_share_a_retriever(prompts, engine):
    import asyncio

    seen = {}

    def factory_for(name):
        def factory(scope):
            seen.setdefault(name, []).append(scope)
            return FakeRetriever(
                chunks=[RetrievedChunk(f"{name}.py", None, 1, 1, name, 1.0)]
            )

        return factory

    async def run(name):
        current_engine.set(
            engine_context(factory_for(name), rag_retrieval_enabled=True)
        )
        await agents.pm_node(pm_state())

    await asyncio.gather(
        asyncio.create_task(run("alpha")), asyncio.create_task(run("beta"))
    )
    users = [user for _, user in prompts]
    assert any("alpha.py" in u and "beta.py" not in u for u in users)
    assert any("beta.py" in u and "alpha.py" not in u for u in users)
    assert set(seen) == {"alpha", "beta"}


# --- The default factory ---------------------------------------------------------------


def test_default_retriever_reads_the_org_from_the_repository_row(monkeypatch):
    from fake_supabase import FakeSupabase

    sb = FakeSupabase()
    sb.tables["repositories"] = [{"id": REPO_ID, "org_id": "org-a"}]
    sb.tables["code_chunks"] = [
        {"org_id": "org-a", "repository_id": REPO_ID, "commit_sha": "a" * 40}
    ]
    monkeypatch.setattr(agents, "get_supabase", lambda: sb)

    r = agents._default_retriever(RetrievalScope(repository_id=REPO_ID))
    assert (r.org_id, r.repository_id) == ("org-a", REPO_ID)
    # Pinned to a commit: only when the index holds exactly that commit.
    held = RetrievalScope(repository_id=REPO_ID, commit_sha="A" * 40)
    assert agents._default_retriever(held) is not None
    other = RetrievalScope(repository_id=REPO_ID, commit_sha="b" * 40)
    assert agents._default_retriever(other) is None
    # An unknown repository, or no database, has no retriever.
    assert agents._default_retriever(RetrievalScope(repository_id=1)) is None
    monkeypatch.setattr(agents, "get_supabase", lambda: None)
    assert agents._default_retriever(RetrievalScope(repository_id=REPO_ID)) is None
