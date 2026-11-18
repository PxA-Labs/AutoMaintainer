"""Spike (#525): can ADK drive Llama 3.3 70B on Groq, through LiteLLM, with
reliable tool calls? The owner's go/no-go step before the ADK Reviewer is
enabled anywhere (docs/design/adk-specialist-agents.md, "Spike").

Manual only; it calls the provider and costs money. Not run by CI or the
test suite. From backend/, with the adk extra installed::

    GROQ_API_KEY=... uv run --extra adk python -m evals.spike_adk_groq --repeats 5
    # the fallback, Gemini on Vertex AI (ADC credentials, no Groq key):
    GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=... \\
    GOOGLE_CLOUD_LOCATION=us-central1 \\
    uv run --extra adk python -m evals.spike_adk_groq --model vertex_ai/gemini-2.5-flash

Each repeat gives the Reviewer's real system prompt and its four read-only
tools (the same declarations ``reviewer_adk`` registers) one small review
task over a throwaway repository: a change that breaks ``ordinal(11)``,
with a failing verification log. The task asks the model to use every tool
once before answering. For each repeat it prints every tool call the model
made and whether it parsed (a registered tool name and a JSON object of
arguments), whether the final answer parses as a ``MaintainerDecision``,
and the verdict (the right one is reject).

GO when every repeat made at least one tool call, every call parsed, every
call named a registered tool, and every final answer parsed; the exit code
is 0 for GO, 1 for NO-GO, 2 for a setup error. Record the output in #525.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import uuid
from pathlib import Path

DEFAULT_MODEL = "llama-3.3-70b-versatile"

BUGGY = '''def ordinal(n: int) -> str:
    """1 -> "st", 2 -> "nd", 11 -> "th"."""
    if n % 10 == 1:
        return "st"
    if n % 10 == 2:
        return "nd"
    if n % 10 == 3:
        return "rd"
    return "th"
'''
DIFF = (
    "--- ordinal.py (modified)\n@@ -1,6 +1,4 @@\n"
    " def ordinal(n: int) -> str:\n"
    '-    if n % 100 in (11, 12, 13):\n-        return "th"\n'
    "     if n % 10 == 1:\n"
)
LOG = (
    "FAILED tests/test_ordinal.py::test_teens - AssertionError: "
    "assert 'st' == 'th'\n1 failed, 3 passed"
)
TASK_SUFFIX = (
    "\n\nBefore answering, call each of your four tools at least once: "
    "get_diff, read_file on ordinal.py, search_code for 'ordinal(' and "
    "get_verification_log."
)


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.spike_adk_groq")
    p.add_argument("--model", default=DEFAULT_MODEL, help="routed model name")
    p.add_argument("--repeats", type=int, default=3)
    args = p.parse_args(argv)
    if args.repeats < 1:
        p.error("--repeats must be at least 1")
    return args


async def one_run(model_name: str, api_key, index: int) -> dict:
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    import agents
    from automaintainer._runtime.specialists import reviewer_adk
    from automaintainer._runtime.specialists.contracts import ReviewRequest
    from automaintainer._runtime.specialists.tools import ReviewTools

    with tempfile.TemporaryDirectory(prefix="spike-adk-") as tmp:
        root = Path(tmp)
        (root / "ordinal.py").write_text(BUGGY, encoding="utf-8")
        tools = ReviewTools(
            root, diff_files=[("ordinal.py", DIFF)], verification_log=LOG
        )
        request = ReviewRequest(
            repository="spike/ordinal",
            pr_number=1,
            head_sha="0" * 40,
            directive="Simplify ordinal() without changing its results.",
            iteration=0,
            verification={"status": "failed", "exit_code": 1, "output_tail": LOG},
            diff=DIFF,
        )
        usage = reviewer_adk._Usage(reviewer_adk.ReviewBudget())
        agent = LlmAgent(
            name=reviewer_adk.AGENT_NAME,
            model=reviewer_adk.build_model(model_name, api_key),
            instruction=lambda _context: reviewer_adk.SYSTEM_PROMPT,
            tools=reviewer_adk._adk_tools(tools, usage),
        )
        sessions = InMemorySessionService()
        runner = Runner(app_name="spike", agent=agent, session_service=sessions)
        session = await sessions.create_session(
            app_name="spike", user_id="spike", session_id=uuid.uuid4().hex
        )
        calls, errors, events = [], [], []
        try:
            async for event in runner.run_async(
                user_id="spike",
                session_id=session.id,
                new_message=types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            text=reviewer_adk._request_prompt(request) + TASK_SUFFIX
                        )
                    ],
                ),
            ):
                events.append(event)
                if event.error_code:
                    errors.append(f"{event.error_code}: {event.error_message}")
                for call in event.get_function_calls():
                    calls.append(
                        {
                            "name": call.name,
                            "args": call.args,
                            "parsed": isinstance(call.args, dict),
                            "registered": call.name in reviewer_adk.TOOL_NAMES,
                        }
                    )
        except Exception as e:  # report, do not hide, a provider failure
            errors.append(f"{type(e).__name__}: {e}")
        finally:
            await runner.close()
        reply = reviewer_adk._final_text(events)
        try:
            verdict = agents.parse_decision(reply, agents.MaintainerDecision).verdict
            parsed = True
        except agents.DecisionParseError as e:
            verdict, parsed = f"unparsed ({e})", False
    return {
        "index": index,
        "calls": calls,
        "errors": errors,
        "decision_parsed": parsed,
        "verdict": verdict,
        "go": bool(calls)
        and all(c["parsed"] and c["registered"] for c in calls)
        and parsed
        and not errors,
    }


async def spike(model_name: str, api_key, repeats: int) -> bool:
    results = []
    for i in range(repeats):
        result = await one_run(model_name, api_key, i)
        results.append(result)
        print(f"run {i}: go={result['go']} verdict={result['verdict']}")
        for call in result["calls"]:
            print(
                f"  call {call['name']} args={call['args']!r} "
                f"parsed={call['parsed']} registered={call['registered']}"
            )
        for error in result["errors"]:
            print(f"  error {error}")
    tools_used = {c["name"] for r in results for c in r["calls"]}
    go = all(r["go"] for r in results)
    print(
        f"\n{model_name}: {sum(r['go'] for r in results)}/{repeats} runs clean; "
        f"tools used: {sorted(tools_used)}; "
        f"right verdict (reject): {sum(r['verdict'] == 'reject' for r in results)}"
        f"/{repeats}"
    )
    print("GO" if go else "NO-GO")
    return go


def main(argv=None) -> int:
    args = parse_args(argv)
    from automaintainer._runtime.specialists import reviewer_adk

    api_key = None
    if not reviewer_adk._is_gemini(args.model):
        api_key = (os.environ.get("GROQ_API_KEY") or "").strip()
        if not api_key:
            print("error: GROQ_API_KEY is not set", file=sys.stderr)
            return 2
    try:
        import google.adk  # noqa: F401
    except ImportError:
        print("error: install the adk extra (uv sync --extra adk)", file=sys.stderr)
        return 2
    return 0 if asyncio.run(spike(args.model, api_key, args.repeats)) else 1


if __name__ == "__main__":  # pragma: no cover - manual spike
    sys.exit(main())
