"""Regression tests: PM / Maintainer decisions are parsed strictly and fail closed."""

from unittest.mock import MagicMock

import pytest

import agents
from agents import END, is_approved_verdict, should_implement, should_iterate


@pytest.mark.parametrize(
    "text",
    [
        '{"verdict": "approve"}',
        '  {"verdict": "approve", "reasons": ["Solid change."]}  ',
    ],
)
def test_unambiguous_approval(text):
    assert is_approved_verdict(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "LGTM",
        "Not LGTM: SQL injection.",
        "I cannot say LGTM",
        "APPROVED",
        "**APPROVED** - good",
        "APPROVED? No. REJECTED.",
        "**VERDICT: APPROVE**",
        "VERDICT: APPROVE? no",
        "VERDICT: APPROVE with changes",
        "VERDICT: REJECT\nVERDICT: APPROVE",
        "VERDICT: APPROVE\nActually, VERDICT: REJECT",
        "Some preamble\nVERDICT: APPROVE",
        # The legacy line format is not a decision any more (#403).
        "VERDICT: APPROVE",
        "  VERDICT: APPROVE  \nSolid change.",
        "verdict: approve\nSolid change.",
        "Verdict: Approve",
        "VERDICT:APPROVE\nok",
        "VERDICT:  APPROVE",
    ],
)
def test_ambiguous_or_negative_is_rejected(text):
    assert is_approved_verdict(text) is False


def test_should_implement_requires_structured_approval():
    approve = '{"verdict": "approve", "reasons": ["Good."]}'
    reject = '{"verdict": "reject", "reasons": ["No."]}'
    assert should_implement({"pm_decision": approve}) == "implementer"
    assert should_implement({"pm_decision": "APPROVED? No. REJECTED."}) == END
    assert should_implement({"pm_decision": reject}) == END
    assert should_implement({"pm_decision": "VERDICT: APPROVE\nGood."}) == END


def test_should_iterate_does_not_stop_on_negated_lgtm():
    assert should_iterate({"review": "Not LGTM: SQL injection.", "iteration": 0}) == (
        "implementer"
    )
    assert should_iterate({"review": '{"verdict": "approve"}', "iteration": 0}) == END


@pytest.mark.asyncio
async def test_not_lgtm_does_not_merge(monkeypatch):
    monkeypatch.setattr(agents, "supabase", None)
    monkeypatch.setenv("MERGE_POLICY", "auto")
    pr = MagicMock()
    repo = MagicMock()
    repo.get_pull.return_value = pr
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=MagicMock(return_value=repo)))

    async def llm(s, u, estimated_tokens=3000):
        return "Not LGTM: SQL injection."

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    await agents.maintainer_node(
        {"code": "x", "repo_name": "o/r", "pr_number": 7, "iteration": 0}
    )
    assert not pr.merge.called


@pytest.mark.asyncio
async def test_targeted_mode_runs_pm_review_and_keeps_human_issue_open(monkeypatch):
    monkeypatch.setattr(agents, "supabase", None)
    issue = MagicMock()
    repo = MagicMock()
    repo.get_issue.return_value = issue
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=MagicMock(return_value=repo)))
    calls = []

    async def llm(s, u, estimated_tokens=3000):
        calls.append(s)
        return '{"verdict": "reject", "reasons": ["Deletes the auth layer."], "blocking_issues": ["Deletes the auth layer."]}'

    monkeypatch.setattr(agents, "run_llm_with_tools", llm)
    result = await agents.pm_node(
        {
            "repo_name": "o/r",
            "idea": "Targeted Issue #3: remove auth.py:1-2",
            "architect_directive": "Targeted Issue #3: remove auth.py:1-2",
            "issue_number": 3,
            "target_issue": 3,
        }
    )
    assert calls, "PM must be consulted in targeted mode"
    assert should_implement(result) == END
    issue.create_comment.assert_called_once()
    issue.edit.assert_not_called()
