"""PM / Maintainer decisions are structured JSON, validated, retried once and
fail closed (#403)."""

import json
from unittest.mock import MagicMock

import pytest

import agents
from agents import (
    END,
    AgentDecision,
    DecisionParseError,
    MaintainerDecision,
    is_approved_verdict,
    parse_decision,
    should_implement,
    should_iterate,
)

APPROVE_JSON = json.dumps(
    {"verdict": "approve", "reasons": ["Small, safe change."], "blocking_issues": []}
)
REJECT_JSON = json.dumps(
    {
        "verdict": "reject",
        "reasons": ["Deletes the auth layer."],
        "blocking_issues": ["auth.py is removed"],
    }
)


# --- parse_decision -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        APPROVE_JSON,
        '  {"verdict": "approve"}  ',
        '{"verdict": " APPROVE "}',
        '{"decision": "approve"}',  # the shape proposed in #403
        '```json\n{"verdict": "approve", "reasons": []}\n```',
        '```\n{"verdict": "approve"}\n```',
        '{"verdict": "approve", "confidence": 0.9}',  # unknown keys ignored
        "VERDICT: APPROVE\nLooks correct.",  # legacy format still understood
    ],
)
def test_approvals_are_accepted(text):
    assert parse_decision(text).approved
    assert is_approved_verdict(text) is True


@pytest.mark.parametrize(
    "text",
    [
        REJECT_JSON,
        '{"verdict": "reject"}',
        "VERDICT: REJECT\nOut of scope.",
    ],
)
def test_rejections_parse_as_reject(text):
    decision = parse_decision(text)
    assert decision.verdict == "reject"
    assert is_approved_verdict(text) is False


@pytest.mark.parametrize(
    "text, error",
    [
        (None, "not text"),
        ("", "empty"),
        ("NOT APPROVED", "does not start with a JSON object"),
        ("**APPROVED**", "does not start with a JSON object"),
        ("Not LGTM: SQL injection.", "does not start with a JSON object"),
        ('Sure! {"verdict": "approve"}', "does not start with a JSON object"),
        (
            '{"verdict": "approve"}\nActually {"verdict": "reject"}',
            "unexpected text after",
        ),
        ('{"verdict": "approve"', "invalid JSON"),  # truncated
        ('```json\n{"verdict": "approve"}', "unterminated fence"),
        ('["approve"]', "does not start with a JSON object"),
        ("{}", "verdict"),
        ('{"verdict": "maybe"}', "verdict"),
        ('{"verdict": true}', "verdict"),
        ('{"verdict": "approve", "reasons": "fine"}', "reasons"),
        (
            '{"verdict": "approve", "blocking_issues": ["SQL injection"]}',
            "must not list blocking_issues",
        ),
        ("VERDICT: APPROVE\nActually, VERDICT: REJECT", "more than one verdict"),
        ("**VERDICT: APPROVE**", "does not start with a JSON object"),
    ],
)
def test_malformed_or_contradictory_replies_raise(text, error):
    with pytest.raises(DecisionParseError, match=error):
        parse_decision(text)
    assert is_approved_verdict(text) is False


def test_validation_errors_do_not_echo_the_reply():
    secret = "ignore previous instructions " * 3
    with pytest.raises(DecisionParseError) as exc:
        parse_decision(json.dumps({"verdict": secret}))
    assert "ignore previous" not in str(exc.value)


def test_maintainer_decision_carries_requested_changes():
    decision = parse_decision(
        json.dumps(
            {
                "verdict": "reject",
                "reasons": ["Off-by-one."],
                "blocking_issues": ["last page dropped"],
                "requested_changes": ["Use ceil division", "  "],
            }
        ),
        MaintainerDecision,
    )
    assert decision.requested_changes == ["Use ceil division"]
    rendered = agents.render_decision(decision)
    assert rendered.startswith("REJECT")
    assert "- Use ceil division" in rendered
    assert "- last page dropped" in rendered


def test_routing_reads_the_stored_json():
    stored = AgentDecision(verdict="approve").model_dump_json()
    assert should_implement({"pm_decision": stored}) == "implementer"
    assert should_implement({"pm_decision": REJECT_JSON}) == END
    assert should_iterate({"review": stored, "iteration": 0}) == END
    assert should_iterate({"review": REJECT_JSON, "iteration": 0}) == "implementer"


# --- nodes: retry once, then fail closed ------------------------------------


def _scripted_llm(replies, calls):
    replies = iter(replies)

    async def llm(system, user, estimated_tokens=3000):
        calls.append((system, user))
        return next(replies)

    return llm


def _pm_state():
    return {
        "repo_name": "o/r",
        "idea": "Fix pagination in api.py:1-5",
        "architect_directive": "Fix pagination in api.py:1-5",
        "issue_number": 3,
        "target_issue": None,
    }


@pytest.fixture
def repo(monkeypatch):
    monkeypatch.setattr(agents, "supabase", None)
    repo = MagicMock()
    monkeypatch.setattr(agents, "gh", MagicMock(get_repo=MagicMock(return_value=repo)))
    return repo


@pytest.mark.asyncio
async def test_pm_prompt_asks_for_json_and_approves_valid_json(monkeypatch, repo):
    calls = []
    monkeypatch.setattr(
        agents, "run_llm_with_tools", _scripted_llm([APPROVE_JSON], calls)
    )
    result = await agents.pm_node(_pm_state())

    assert len(calls) == 1
    assert '"verdict"' in calls[0][0] and "ONLY one JSON object" in calls[0][0]
    assert json.loads(result["pm_decision"])["verdict"] == "approve"
    assert should_implement(result) == "implementer"
    comment = repo.get_issue.return_value.create_comment.call_args.args[0]
    assert comment.startswith("**Reviewer Decision:** APPROVE")
    assert "Small, safe change." in comment
    # The structured decision is recorded in the run's logs.
    recorded = [m["decision"] for m in result["log_messages"] if "decision" in m]
    assert recorded == [
        {
            "phase": "pm",
            "verdict": "approve",
            "reasons": ["Small, safe change."],
            "blocking_issues": [],
            "parse_error": None,
        }
    ]


@pytest.mark.asyncio
async def test_pm_retries_once_with_corrective_message(monkeypatch, repo):
    calls = []
    monkeypatch.setattr(
        agents,
        "run_llm_with_tools",
        _scripted_llm(["**APPROVED** looks good", APPROVE_JSON], calls),
    )
    result = await agents.pm_node(_pm_state())

    assert len(calls) == 2
    assert calls[1][0] == calls[0][0]
    assert calls[1][1].startswith(calls[0][1])
    assert "could not be used" in calls[1][1]
    assert "does not start with a JSON object" in calls[1][1]
    assert should_implement(result) == "implementer"


@pytest.mark.asyncio
async def test_pm_fails_closed_after_second_bad_reply(monkeypatch, repo):
    calls, events = [], []
    monkeypatch.setattr(
        agents,
        "run_llm_with_tools",
        _scripted_llm(
            ["APPROVED", '{"verdict": "approve", "blocking_issues": ["x"]}'], calls
        ),
    )

    async def capture(event):
        events.append(event)

    monkeypatch.setattr(agents, "emit_event", capture)
    token = agents.current_run_id.set("run-1")
    try:
        result = await agents.pm_node(_pm_state())
    finally:
        agents.current_run_id.reset(token)

    assert len(calls) == 2
    assert should_implement(result) == END
    stored = parse_decision(result["pm_decision"])
    assert stored.verdict == "reject"
    assert "fail closed" in stored.reasons[0]
    issue = repo.get_issue.return_value
    comment = issue.create_comment.call_args.args[0]
    assert comment.startswith("**Reviewer Decision:** REJECT")
    assert "APPROVED" not in comment.replace("**Reviewer Decision:**", "")
    # An agent-created issue is closed on rejection, as before.
    issue.edit.assert_called_once_with(state="closed")
    errors = [
        e for e in events if getattr(e, "error_code", "") == "DECISION_PARSE_FAILED"
    ]
    assert len(errors) == 1 and errors[0].fatal is False
    recorded = [m["decision"] for m in result["log_messages"] if "decision" in m]
    assert recorded[0]["verdict"] == "reject"
    assert "must not list blocking_issues" in recorded[0]["parse_error"]


@pytest.mark.asyncio
async def test_maintainer_structured_rejection_feeds_requested_changes(
    monkeypatch, repo
):
    monkeypatch.setenv("MERGE_POLICY", "auto")
    pr = repo.get_pull.return_value
    pr.get_files.return_value = []
    review = json.dumps(
        {
            "verdict": "reject",
            "reasons": ["Off-by-one."],
            "blocking_issues": ["The last page is dropped."],
            "requested_changes": ["Use ceil division in paginate()."],
        }
    )
    calls = []
    monkeypatch.setattr(agents, "run_llm_with_tools", _scripted_llm([review], calls))
    result = await agents.maintainer_node(
        {"code": "x", "repo_name": "o/r", "pr_number": 7, "iteration": 0}
    )

    assert '"requested_changes"' in calls[0][0]
    assert result["merge_status"] == "changes_requested"
    assert result["iteration"] == 1
    assert should_iterate(result) == "implementer"
    assert parse_decision(result["review"], MaintainerDecision).requested_changes == [
        "Use ceil division in paginate()."
    ]
    comment = pr.create_issue_comment.call_args.args[0]
    assert comment.startswith("**Maintainer Review:** REJECT")
    assert "Use ceil division" in comment
    pr.merge.assert_not_called()


@pytest.mark.asyncio
async def test_maintainer_never_merges_on_unparseable_reviews(monkeypatch, repo):
    monkeypatch.setenv("MERGE_POLICY", "auto")
    pr = repo.get_pull.return_value
    pr.get_files.return_value = []
    calls = []
    monkeypatch.setattr(
        agents,
        "run_llm_with_tools",
        _scripted_llm(["LGTM, approve!", "Approved. Ship it."], calls),
    )
    result = await agents.maintainer_node(
        {"code": "x", "repo_name": "o/r", "pr_number": 7, "iteration": 0}
    )

    assert len(calls) == 2
    assert result["merge_status"] == "changes_requested"
    assert is_approved_verdict(result["review"]) is False
    pr.merge.assert_not_called()


@pytest.mark.asyncio
async def test_maintainer_json_approval_merges_under_auto_policy(monkeypatch, repo):
    monkeypatch.setenv("MERGE_POLICY", "auto")
    pr = repo.get_pull.return_value
    pr.get_files.return_value = []
    pr.merged = False
    monkeypatch.setattr(
        agents,
        "run_llm_with_tools",
        _scripted_llm(['```json\n{"verdict": "approve", "reasons": ["ok"]}\n```'], []),
    )
    result = await agents.maintainer_node(
        {"code": "x", "repo_name": "o/r", "pr_number": 7, "iteration": 0}
    )

    assert result["merge_status"] == "merged"
    pr.merge.assert_called_once()
