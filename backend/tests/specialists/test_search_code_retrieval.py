"""The ADK Reviewer's search_code over the RAG index (#525/#526).

With retrieval on and the index holding the PR head commit, search_code asks
the retriever (scoped to the run's repository and that commit) instead of
ripgrep. In every other case (flag off, nothing indexed, the index at
another commit, the retriever failing or finding nothing) it is ripgrep,
exactly as before. Arguments are checked the same way either way, and the
tool still counts against the review's budgets.
"""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import agents
from adk_fakes import APPROVE, ModelFactory, calls, needs_adk, scripted
from automaintainer import config as config_module
from automaintainer._runtime.retrieval import RetrievalScope, RetrievedChunk
from automaintainer._specialists import reviewer_adk
from automaintainer._specialists.reviewer_adk import ReviewBudget
from automaintainer._specialists.tools import (
    TRUNCATED,
    ReviewTools,
    ToolLimits,
    ToolRefused,
)
from test_agent_merge_policy import _setup, _state

HEAD = "headsha"
REPO_ID = 99
CHUNKS = [
    RetrievedChunk(
        "pkg/ints.py", "parse_int_list", 3, 5, "def parse_int_list(s):\n    ...", 0.91
    ),
    RetrievedChunk("README.md", None, 1, 2, "# Ints\nParses lists.", 0.42),
]


class FakeRetriever:
    def __init__(self, chunks=CHUNKS, error=None):
        self.chunks, self.error = chunks, error
        self.calls = []

    def retrieve(self, query, k=5):
        self.calls.append((query, k))
        if self.error is not None:
            raise self.error
        return list(self.chunks[:k])


@pytest.fixture
def clone(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    (root / "app.py").write_text("x = 2\n", encoding="utf-8")
    return root


def make_tools(clone, retriever=None, **limits):
    return ReviewTools(
        clone,
        diff_files=[],
        verification_log="",
        limits=ToolLimits(**limits),
        retriever=retriever,
        search_k=8,
    )


def ripgrep_stub(tools, answer="app.py:1:x = 2"):
    seen = []

    def rg(query, regex=False):
        seen.append((query, regex))
        return answer

    tools._ripgrep = rg
    return seen


# --- ReviewTools --------------------------------------------------------------------


def test_with_a_retriever_search_code_returns_the_nearest_chunks(clone):
    retriever = FakeRetriever()
    tools = make_tools(clone, retriever)
    rg = ripgrep_stub(tools)

    text = tools.search_code("where are int lists parsed")

    assert retriever.calls == [("where are int lists parsed", 8)]
    assert rg == []
    assert text.startswith("2 code index matches")
    assert "pkg/ints.py lines 3-5 (parse_int_list)\n3| def parse_int_list(s):" in text
    assert "README.md lines 1-2\n1| # Ints\n2| Parses lists." in text


def test_k_is_bounded_by_the_search_result_limit(clone):
    retriever = FakeRetriever()
    tools = make_tools(clone, retriever, max_search_results=1)
    ripgrep_stub(tools)
    tools.search_code("ints")
    assert retriever.calls[0][1] == 1


def test_index_output_is_capped(clone):
    big = [RetrievedChunk("big.py", None, 1, 900, "z\n" * 900, 1.0)]
    tools = make_tools(clone, FakeRetriever(chunks=big), max_output_chars=500)
    ripgrep_stub(tools)
    text = tools.search_code("z")
    assert len(text) <= 500 + len(TRUNCATED) and text.endswith(TRUNCATED)


@pytest.mark.parametrize("query", ["", "a" * 201, "two\nlines", "nul\x00"])
def test_arguments_are_refused_before_the_retriever_is_asked(clone, query):
    retriever = FakeRetriever()
    tools = make_tools(clone, retriever)
    with pytest.raises(ToolRefused):
        tools.search_code(query)
    assert retriever.calls == []


def test_a_regex_search_stays_with_ripgrep(clone):
    retriever = FakeRetriever()
    tools = make_tools(clone, retriever)
    rg = ripgrep_stub(tools)
    assert tools.search_code("x\\s=", regex=True) == "app.py:1:x = 2"
    assert retriever.calls == [] and rg == [("x\\s=", True)]


@pytest.mark.parametrize(
    "retriever", [FakeRetriever(error=RuntimeError("db")), FakeRetriever(chunks=[])]
)
def test_a_failing_or_empty_index_falls_back_to_ripgrep(clone, retriever):
    tools = make_tools(clone, retriever)
    rg = ripgrep_stub(tools)
    assert tools.search_code("x = 2") == "app.py:1:x = 2"
    assert rg == [("x = 2", False)]


def test_without_a_retriever_it_is_ripgrep(clone):
    tools = ReviewTools(clone, diff_files=[], verification_log="")
    rg = ripgrep_stub(tools)
    assert tools.search_code("x = 2") == "app.py:1:x = 2"
    assert rg == [("x = 2", False)]


# --- review_pull_request: which retriever the tools get ------------------------------


@pytest.fixture
def head(tmp_path, monkeypatch):
    root = tmp_path / "head"
    root.mkdir()
    (root / "app.py").write_text("x = 2\n", encoding="utf-8")

    @asynccontextmanager
    async def checkout(clone_url, branch, head_sha):
        yield Path(root)

    monkeypatch.setattr(reviewer_adk, "checkout_head", checkout)
    monkeypatch.setattr(config_module, "_adk_missing", lambda: None)
    for name in ("RAG_RETRIEVAL_ENABLED", "RAG_TOP_K"):
        monkeypatch.delenv(name, raising=False)
    return root


@pytest.fixture
def captured(monkeypatch):
    """The ReviewTools each review gets (the review itself is stubbed)."""
    seen = []

    async def review(request, tools, **kwargs):
        seen.append(tools)
        return agents.MaintainerDecision(verdict="approve", reasons=["ok"]), None

    monkeypatch.setattr(reviewer_adk, "review", review)
    return seen


@pytest.fixture
def scopes(monkeypatch):
    state = {"scopes": [], "retriever": FakeRetriever()}

    def factory(scope):
        state["scopes"].append(scope)
        return state["retriever"]

    monkeypatch.setattr(agents, "_default_retriever", factory)
    return state


async def review_once(monkeypatch):
    gh_repo, pr, _ = _setup(monkeypatch)
    pr.head.sha = HEAD
    pr.head.ref = "automaintainer/run-1"
    pr.get_files = lambda: []
    state = {**_state(), "repository_id": REPO_ID}
    return await reviewer_adk.review_pull_request(state, gh_repo, pr, "diff")


async def test_flag_off_the_tools_have_no_retriever(
    monkeypatch, head, captured, scopes
):
    await review_once(monkeypatch)
    (tools,) = captured
    assert tools.retriever is None
    assert scopes["scopes"] == []


async def test_flag_on_the_retriever_is_scoped_to_the_head_commit(
    monkeypatch, head, captured, scopes
):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")
    monkeypatch.setenv("RAG_TOP_K", "4")
    await review_once(monkeypatch)
    (tools,) = captured
    assert scopes["scopes"] == [RetrievalScope(repository_id=REPO_ID, commit_sha=HEAD)]
    assert tools.retriever is scopes["retriever"]
    assert tools.search_k == 4


async def test_head_not_indexed_means_ripgrep(monkeypatch, head, captured, scopes):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")
    scopes["retriever"] = None  # the factory: the index is at another commit
    await review_once(monkeypatch)
    assert captured[0].retriever is None


async def test_resolving_the_retriever_failing_means_ripgrep(
    monkeypatch, head, captured, caplog
):
    monkeypatch.setenv("RAG_RETRIEVAL_ENABLED", "1")

    def broken(scope):
        raise ConnectionError("down")

    monkeypatch.setattr(agents, "_default_retriever", broken)
    decision, _ = await review_once(monkeypatch)
    assert captured[0].retriever is None
    assert decision.approved  # the review itself went ahead
    assert "ConnectionError" in caplog.text


# --- through ADK, with the budgets unchanged ------------------------------------------


@needs_adk
async def test_search_code_through_adk_uses_the_index_and_counts_as_a_tool_call(
    monkeypatch, clone
):
    async def no_key(model_name, estimated_tokens):
        return None

    monkeypatch.setattr(reviewer_adk, "_acquire_key", no_key)
    factory = ModelFactory(
        policy=scripted(calls(("search_code", {"query": "parse ints"})), APPROVE)
    )
    monkeypatch.setattr(reviewer_adk, "build_model", factory)
    tools = make_tools(clone, FakeRetriever())
    from automaintainer._specialists.contracts import ReviewRequest

    request = ReviewRequest(
        repository="o/r", pr_number=1, head_sha=HEAD, iteration=0, diff="d"
    )

    decision, _ = await reviewer_adk.review(
        request, tools, budget=ReviewBudget(max_tool_calls=1)
    )
    assert decision.approved
    (result,) = factory.turns[-1].tool_results
    assert '<untrusted-data source="search_code">' in result
    assert "pkg/ints.py lines 3-5 (parse_int_list)" in result

    # One more call than the budget allows is still a rejection.
    factory.policy = scripted(
        calls(("search_code", {"query": "a"}), ("search_code", {"query": "b"}))
    )
    decision, _ = await reviewer_adk.review(
        request, tools, budget=ReviewBudget(max_tool_calls=1)
    )
    assert decision.verdict == "reject"
    assert "tool calls" in decision.reasons[0]


def test_the_default_budgets_are_unchanged():
    assert ReviewBudget() == ReviewBudget(
        max_tool_calls=8,
        max_model_calls=11,
        max_tokens=60_000,
        max_output_tokens=4096,
        timeout_seconds=180.0,
        estimated_tokens=3000,
    )
