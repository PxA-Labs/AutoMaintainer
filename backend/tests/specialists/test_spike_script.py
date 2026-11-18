"""The ADK/Groq spike script (#525) without a provider: its setup checks, and
its GO/NO-GO logic driven by a fake ADK model. The real spike is the owner's
manual step (it calls Groq); nothing here does."""

import pytest

from adk_fakes import REJECT, ModelFactory, calls, needs_adk, scripted
from automaintainer._specialists import reviewer_adk
from evals import spike_adk_groq as spike


def test_refuses_to_start_without_a_groq_key(monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert spike.main([]) == 2
    assert "GROQ_API_KEY" in capsys.readouterr().err


def test_parse_args_defaults():
    args = spike.parse_args([])
    assert args.model == "llama-3.3-70b-versatile" and args.repeats == 3
    with pytest.raises(SystemExit):
        spike.parse_args(["--repeats", "0"])


FOUR_TOOLS = calls(
    ("get_diff", {}),
    ("read_file", {"path": "ordinal.py"}),
    ("search_code", {"query": "ordinal("}),
    ("get_verification_log", {}),
)


@needs_adk
async def test_go_when_tool_calls_and_the_answer_parse(monkeypatch, capsys):
    factory = ModelFactory(policy=scripted(FOUR_TOOLS, REJECT))
    monkeypatch.setattr(reviewer_adk, "build_model", factory)

    assert await spike.spike("llama-3.3-70b-versatile", "k", 2) is True

    out = capsys.readouterr().out
    assert out.rstrip().endswith("GO")
    assert "2/2 runs clean" in out and "right verdict (reject): 2/2" in out
    assert factory.built[0].api_key == "k"


@needs_adk
async def test_no_go_when_the_answer_does_not_parse(monkeypatch, capsys):
    factory = ModelFactory(policy=scripted(FOUR_TOOLS, "LGTM"))
    monkeypatch.setattr(reviewer_adk, "build_model", factory)

    assert await spike.spike("llama-3.3-70b-versatile", "k", 1) is False
    assert capsys.readouterr().out.rstrip().endswith("NO-GO")


@needs_adk
async def test_no_go_without_any_tool_call(monkeypatch, capsys):
    monkeypatch.setattr(
        reviewer_adk, "build_model", ModelFactory(policy=scripted(REJECT))
    )
    assert await spike.spike("llama-3.3-70b-versatile", "k", 1) is False
