"""The ADK Reviewer (#525) with a fake ADK model: no network, no Groq.

The real ADK runner, session and tool dispatch run; ``build_model`` is
replaced by ``ModelFactory`` and the API key lookup by a stub. Every way the
review can go wrong (a budget, a timeout, an exception, an invalid answer,
a cancellation) must end in a rejection that says why, never an approval.
"""

import asyncio
import subprocess
import sys

import pytest

import agents
from adk_fakes import APPROVE, REJECT, ModelFactory, calls, scripted
from automaintainer._runtime.specialists import reviewer_adk
from automaintainer._runtime.specialists.contracts import ReviewRequest
from automaintainer._runtime.specialists.reviewer_adk import ReviewBudget
from automaintainer._runtime.specialists.tools import ReviewTools
from events import ErrorEvent, TelemetryEvent

SHA = "b" * 40
TOOL_NAMES = {"get_diff", "read_file", "search_code", "get_verification_log"}


@pytest.fixture
def clone(tmp_path):
    root = tmp_path / "clone"
    root.mkdir()
    (root / "app.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("TOP SECRET\n", encoding="utf-8")
    return root


@pytest.fixture
def review_tools(clone):
    return ReviewTools(
        clone,
        diff_files=[("app.py", "--- app.py (modified)\n-x = 1\n+x = 2")],
        verification_log="status=passed\n1 passed",
    )


@pytest.fixture
def factory(monkeypatch):
    """Install a ModelFactory; ``factory.policy`` is set by each test."""
    made = ModelFactory(policy=scripted(APPROVE))
    monkeypatch.setattr(reviewer_adk, "build_model", made)

    async def no_key(model_name):
        return None

    monkeypatch.setattr(reviewer_adk, "_acquire_key", no_key)
    return made


class Events:
    def __init__(self):
        self.events = []

    async def emit(self, event):
        self.events.append(event)


@pytest.fixture
def events():
    sink = Events()
    tokens = [
        agents.current_run_id.set("run-adk"),
        agents.current_emitter.set(sink),
    ]
    yield sink
    agents.current_emitter.reset(tokens[1])
    agents.current_run_id.reset(tokens[0])


def request(**overrides):
    values = dict(
        repository="o/r",
        pr_number=5,
        head_sha=SHA,
        directive="Make x equal 2.",
        iteration=0,
        verification={"status": "passed", "exit_code": 0, "output_tail": "1 passed"},
        diff="--- app.py (modified)\n-x = 1\n+x = 2",
    )
    values.update(overrides)
    return ReviewRequest(**values)


async def run_review(review_tools, budget=None, **overrides):
    kwargs = {"budget": budget} if budget else {}
    return await reviewer_adk.review(request(**overrides), review_tools, **kwargs)


def assert_rejected(decision, why):
    assert decision.verdict == "reject"
    assert not decision.approved
    assert any(why in r for r in decision.reasons), decision.reasons
    assert decision.blocking_issues


# --- approve and reject ------------------------------------------------------------


async def test_approve_after_reading_the_changed_file(factory, review_tools):
    factory.policy = scripted(calls(("read_file", {"path": "app.py"})), APPROVE)

    decision, parse_error = await run_review(review_tools)

    assert decision.approved and parse_error is None
    first, second = factory.turns
    assert set(first.tools) == TOOL_NAMES
    assert first.system.startswith("You are the Maintainer")
    assert agents.UNTRUSTED_DATA_NOTICE in first.system
    assert agents.MAINTAINER_DECISION_INSTRUCTIONS in first.system
    # The request: untrusted parts inside blocks, the SHA and PR outside.
    prompt = first.user[0]
    assert f"#5 of o/r at head {SHA}" in prompt
    assert '<untrusted-data source="originating issue">' in prompt
    assert '<untrusted-data source="pr diff">' in prompt
    assert "Verification of this iteration: status=passed" in prompt
    # The tool result reached the model as untrusted data.
    (result,) = second.tool_results
    assert '<untrusted-data source="read_file app.py">' in result
    assert "1| x = 2" in result


async def test_reject_carries_the_requested_changes(factory, review_tools):
    factory.policy = scripted(REJECT)

    decision, parse_error = await run_review(review_tools)

    assert decision.verdict == "reject" and parse_error is None
    assert decision.requested_changes == ["set x = 2"]


async def test_every_tool_works_through_adk(factory, review_tools):
    factory.policy = scripted(
        calls(
            ("get_diff", {"path": "app.py"}),
            ("get_verification_log", {}),
            ("read_file", {"path": "app.py", "start": 1, "end": 1}),
        ),
        APPROVE,
    )

    decision, _ = await run_review(review_tools)

    assert decision.approved
    results = "\n".join(factory.turns[-1].tool_results)
    assert "+x = 2" in results and "1 passed" in results and "1| x = 2" in results


# --- fail closed -------------------------------------------------------------------


async def test_tool_call_budget_exhaustion_rejects(factory, review_tools):
    factory.policy = scripted(calls(("read_file", {"path": "app.py"})))

    decision, parse_error = await run_review(review_tools)

    assert_rejected(decision, "8 tool calls")
    assert parse_error is None
    assert len(factory.turns) == 9  # 8 calls served, the 9th refused


async def test_parallel_tool_calls_count_against_the_budget(factory, review_tools):
    factory.policy = scripted(calls(*[("get_verification_log", {})] * 3), APPROVE)

    decision, _ = await run_review(review_tools, ReviewBudget(max_tool_calls=2))

    assert_rejected(decision, "2 tool calls")


async def test_token_budget_exhaustion_rejects(factory, review_tools):
    factory.tokens_per_turn = 5000
    factory.policy = scripted(calls(("get_diff", {})), APPROVE)

    decision, _ = await run_review(review_tools, ReviewBudget(max_tokens=8000))

    assert_rejected(decision, "token budget")


async def test_timeout_rejects(factory, review_tools):
    factory.delay = 5
    decision, _ = await run_review(review_tools, ReviewBudget(timeout_seconds=0.2))
    assert_rejected(decision, "timed out after 0.2s")


async def test_model_exception_rejects_without_echoing_it(factory, review_tools):
    factory.policy = scripted(RuntimeError("provider said gsk_secret"))

    decision, _ = await run_review(review_tools)

    assert_rejected(decision, "RuntimeError")
    assert "gsk_secret" not in decision.model_dump_json()


async def test_invalid_output_twice_rejects(factory, review_tools, events):
    factory.policy = scripted("LGTM, ship it", "VERDICT: APPROVE")

    decision, parse_error = await run_review(review_tools)

    assert_rejected(decision, "could not be parsed after one retry")
    assert parse_error
    assert len(factory.turns) == 2
    retry = factory.turns[1].user[-1]
    assert retry.startswith("Your previous reply could not be used")


async def test_invalid_output_once_gets_one_corrective_retry(factory, review_tools):
    factory.policy = scripted("not json", APPROVE)

    decision, parse_error = await run_review(review_tools)

    assert decision.approved and parse_error is None


async def test_approval_with_blockers_is_invalid_not_an_approval(factory, review_tools):
    bad = '{"verdict": "approve", "blocking_issues": ["x"]}'
    factory.policy = scripted(bad, bad)
    decision, parse_error = await run_review(review_tools)
    assert_rejected(decision, "could not be parsed")
    assert "blocking_issues" in parse_error


async def test_run_stop_requested_during_the_review_rejects(factory, review_tools):
    stop = {"now": False}

    def policy(turn, turns):
        stop["now"] = True  # engine.cancel(run_id) while the model thinks
        return calls(("get_diff", {}))

    factory.policy = policy
    token = agents._run_should_stop.set(lambda: stop["now"])
    try:
        decision, _ = await run_review(review_tools)
    finally:
        agents._run_should_stop.reset(token)

    assert_rejected(decision, "cancelled")
    assert len(factory.turns) == 1


async def test_cancelling_the_task_propagates_and_never_approves(factory, review_tools):
    factory.delay = 5
    factory.policy = scripted(APPROVE)
    task = asyncio.create_task(run_review(review_tools))
    while not factory.turns:
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_internal_cancellation_rejects(factory, review_tools):
    """A CancelledError that is not the task's own cancellation (the
    provider client cancelled a request) is a rejection, not a crash."""
    factory.policy = scripted(asyncio.CancelledError())

    decision, _ = await run_review(review_tools)

    assert_rejected(decision, "cancelled")


# --- the tools under an obedient model ---------------------------------------------


async def test_path_escape_through_the_model_is_refused(factory, review_tools):
    factory.policy = scripted(
        calls(
            ("read_file", {"path": "../outside.txt"}),
            ("read_file", {"path": "/etc/passwd"}),
            ("get_diff", {"path": "../../x", "page": 1}),
        ),
        APPROVE,
    )

    decision, _ = await run_review(review_tools)

    results = factory.turns[-1].tool_results
    assert len(results) == 3
    assert all("refused" in r for r in results)
    assert "TOP SECRET" not in "".join(t.everything for t in factory.turns)
    assert decision.approved  # refusals cost tool calls, not the review


async def test_ripgrep_flag_injection_through_the_model_is_literal(
    factory, review_tools, clone
):
    import shutil

    if shutil.which("rg") is None:
        pytest.skip("ripgrep missing")
    factory.policy = scripted(
        calls(("search_code", {"query": "--files"})),
        calls(("search_code", {"query": "--pre=sh"})),
        APPROVE,
    )

    await run_review(review_tools)

    first, second = (t.tool_results[-1] for t in factory.turns[1:])
    assert "no matches" in first and "app.py" not in first
    assert "no matches" in second


async def test_unknown_tool_rejects(factory, review_tools):
    factory.policy = scripted(calls(("merge_pull_request", {"pr": 5})), APPROVE)
    decision, _ = await run_review(review_tools)
    assert decision.verdict == "reject"


# --- accounting and tracing ----------------------------------------------------------


async def test_token_usage_is_added_to_the_run(factory, review_tools, events):
    factory.policy = scripted(calls(("get_diff", {})), APPROVE)

    await run_review(review_tools)

    (telemetry,) = [e for e in events.events if isinstance(e, TelemetryEvent)]
    assert telemetry.run_id == "run-adk"
    assert telemetry.tokens_used == 200
    assert telemetry.model_name == "llama-3.3-70b-versatile"


async def test_a_failed_review_emits_a_non_fatal_error(factory, review_tools, events):
    factory.policy = scripted(RuntimeError("boom"))

    await run_review(review_tools)

    (error,) = [e for e in events.events if isinstance(e, ErrorEvent)]
    assert error.error_code == "SPECIALIST_REVIEW_FAILED" and not error.fatal


async def test_review_and_tool_calls_are_spans(factory, review_tools, monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    import observability

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(
        observability,
        "get_tracer",
        lambda name="automaintainer": provider.get_tracer(name),
    )
    factory.policy = scripted(
        calls(("read_file", {"path": "app.py"}), ("get_diff", {})), APPROVE
    )

    await run_review(review_tools)

    spans = {s.name: s for s in exporter.get_finished_spans()}
    review = spans["specialist.reviewer"]
    assert review.attributes["reviewer.verdict"] == "approve"
    assert review.attributes["reviewer.tool_calls"] == 2
    assert review.attributes["reviewer.tokens"] == 200
    tools = [s for s in exporter.get_finished_spans() if s.name == "specialist.tool"]
    assert sorted(s.attributes["tool.name"] for s in tools) == ["get_diff", "read_file"]
    for span in tools:
        assert span.context.trace_id == review.context.trace_id
        ancestor, by_id = span, {
            s.context.span_id: s for s in exporter.get_finished_spans()
        }
        while ancestor.parent is not None and ancestor is not review:
            ancestor = by_id.get(ancestor.parent.span_id, review)
        assert ancestor is review


# --- model and credentials ------------------------------------------------------------


def test_build_model_routes_groq_models_through_litellm():
    model = reviewer_adk.build_model("llama-3.3-70b-versatile", "gsk-test")
    assert type(model).__name__ == "LiteLlm"
    assert model.model == "groq/llama-3.3-70b-versatile"


def test_build_model_uses_gemini_natively():
    model = reviewer_adk.build_model("gemini-2.5-pro", None)
    assert type(model).__name__ == "Gemini" and model.model == "gemini-2.5-pro"
    vertex = reviewer_adk.build_model("vertex_ai/gemini-2.5-flash", None)
    assert type(vertex).__name__ == "Gemini" and vertex.model == "gemini-2.5-flash"


def test_importing_the_reviewer_does_not_import_adk():
    code = (
        "import sys, automaintainer._runtime.specialists.reviewer_adk as r; "
        "print(any(m.startswith('google.adk') for m in sys.modules))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "False"
