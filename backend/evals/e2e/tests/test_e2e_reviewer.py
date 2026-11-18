"""The Reviewer measurement: a known change on a PR, the real maintainer node
reviews it with the engine's real verification result in its prompt."""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from evals.e2e.policies import (
    LiveTransport,
    ScriptedTransport,
    noop_reviewer,
    oracle_reviewer,
)
from evals.e2e.runner import run_review_case
from evals.e2e.taskset import load_e2e_tasks, load_review_cases

ITEM = load_e2e_tasks(["inflection_ordinal_teens"])[0]
BAD, GOOD = load_review_cases(
    ["bad_ordinal_partial_teens", "good_inflection_ordinal_teens"]
)


@pytest.fixture(autouse=True)
def _verify_with_this_python(monkeypatch):
    monkeypatch.setenv("VERIFY_PYTHON", sys.executable)


def review(case, policy, tmp_path):
    transport = ScriptedTransport(policy(case))
    record = asyncio.run(
        run_review_case(ITEM, case, transport=transport, root=tmp_path)
    )
    return record, transport


def test_oracle_reviewer_rejects_the_bad_change(tmp_path):
    record, transport = review(BAD, oracle_reviewer, tmp_path)
    assert record["error"] is None
    assert record["verdict"] == "reject" and record["correct"] is True
    assert record["defect_class"] == "missing-edge-case"
    # Only the Maintainer is a model call; the review saw the PR's diff and
    # a verification the engine ran on the pushed head.
    [call] = transport.calls
    assert call["role"] == "maintainer"
    assert record["verification"] == "passed"


def test_rubber_stamp_reviewer_approves_the_bad_change(tmp_path):
    record, _ = review(BAD, noop_reviewer, tmp_path)
    assert record["verdict"] == "approve" and record["correct"] is False


def test_correct_change_is_approved_by_the_oracle_reviewer(tmp_path):
    record, _ = review(GOOD, oracle_reviewer, tmp_path)
    assert record["verdict"] == "approve" and record["correct"] is True


def test_maintainer_prompt_contains_the_change(tmp_path):
    seen = []

    def policy(case):
        def answer(role, system, user):
            seen.append(user)
            return oracle_reviewer(case)(role, system, user)

        return answer

    review(BAD, policy, tmp_path)
    assert "(11, 12)" in seen[0]
    assert "status=passed" in seen[0]


def test_live_transport_reports_usage_without_network():
    class FakeChat:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def ainvoke(self, messages):
            return SimpleNamespace(
                content="answer",
                response_metadata={"finish_reason": "stop"},
                usage_metadata={
                    "input_tokens": 12,
                    "output_tokens": 3,
                    "total_tokens": 15,
                },
            )

    made = []

    def factory(**kwargs):
        made.append(kwargs)
        return FakeChat(**kwargs)

    transport = LiveTransport(chat_factory=factory)
    reply = asyncio.run(
        transport(system_prompt="s", user_prompt="u", model="m", api_key="k")
    )
    assert reply == "answer" and reply.finish_reason == "stop"
    assert transport.calls[0]["usage"]["input_tokens"] == 12
    assert made == [{"model": "m", "api_key": "k", "temperature": 0.0, "seed": 1234}]
