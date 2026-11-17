"""The model side of an e2e run: injected LLM transports and scripted agents.

A transport is what ``AutoMaintainerEngine(llm=...)`` calls for every model
call: ``await transport(system_prompt=, user_prompt=, model=, api_key=)``.
The engine's rate limiter and key rotation still apply around it. Each
transport records every call (role, normalised prompt hash, answer, usage,
latency), which is what cassettes store.

- ``ScriptedTransport``: answers from a policy ``(role, system, user) -> text``;
  the oracle and no-op agents below, and test doubles.
- ``ReplayTransport``: answers from a cassette, in order.
- ``LiveTransport``: Groq through ``langchain_groq.ChatGroq`` with the key
  the engine's rate limiter hands it; reports token usage.

The role is the agent role the engine set for the call (``agents._llm_role``,
the same value that selects the routed model).
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

from agents import LLMResponse, _llm_role

from .cassettes import prompt_hash
from .taskset import E2ETask, ReviewCase

APPROVE = json.dumps(
    {
        "verdict": "approve",
        "reasons": ["The change implements the issue."],
        "blocking_issues": [],
    }
)
REJECT = json.dumps(
    {
        "verdict": "reject",
        "reasons": ["The change does not resolve the issue."],
        "blocking_issues": ["The behaviour described in the issue is still wrong."],
    }
)

Policy = Callable[[str, str, str], str]


class CassetteError(RuntimeError):
    """A replayed run asked for a call its cassette does not have."""


class _Transport:
    def __init__(self):
        self.calls: list[dict] = []
        # A harness failure (not a model answer) seen during the run.
        self.harness_error: Optional[str] = None

    async def __call__(self, *, system_prompt, user_prompt, model, api_key):
        role = _llm_role.get() or "unknown"
        started = time.perf_counter()
        try:
            text, finish_reason, usage, extra = await self._answer(
                role, system_prompt, user_prompt, model, api_key
            )
        except CassetteError as e:
            self.harness_error = self.harness_error or str(e)
            raise
        call = {
            "role": role,
            "model": model,
            "prompt_sha256": prompt_hash(system_prompt, user_prompt),
            "response": text,
            "finish_reason": finish_reason,
            "usage": usage,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            **extra,
        }
        self.calls.append(call)
        return LLMResponse(text, finish_reason)

    async def _answer(self, role, system_prompt, user_prompt, model, api_key):
        raise NotImplementedError


class ScriptedTransport(_Transport):
    def __init__(self, policy: Policy, usage: Optional[dict] = None):
        super().__init__()
        self.policy = policy
        self.usage = usage

    async def _answer(self, role, system_prompt, user_prompt, model, api_key):
        text = self.policy(role, system_prompt, user_prompt)
        usage = dict(self.usage) if self.usage else None
        return text, "stop", usage, {}


class ReplayTransport(_Transport):
    """Answers the calls of one recorded run in order.

    A call whose prompt hash differs from the recorded one is answered anyway
    and counted in ``stale``; a role mismatch or a missing answer is a
    harness error, because the run took a different path than the recording.
    """

    def __init__(self, cassette: dict, repeat: int = 0):
        super().__init__()
        runs = cassette.get("runs") or []
        if not runs:
            raise CassetteError(f"cassette {cassette.get('id')!r} has no runs")
        self.cassette_id = cassette.get("id")
        self.recorded = runs[repeat % len(runs)]
        self.model = cassette.get("model")
        self.position = 0
        self.stale = 0

    async def _answer(self, role, system_prompt, user_prompt, model, api_key):
        if self.position >= len(self.recorded):
            raise CassetteError(
                f"cassette {self.cassette_id!r} has no answer for call "
                f"{self.position + 1} ({role})"
            )
        call = self.recorded[self.position]
        if call.get("role") != role:
            raise CassetteError(
                f"cassette {self.cassette_id!r} call {self.position + 1} was "
                f"recorded for {call.get('role')!r}, the run asked {role!r}"
            )
        self.position += 1
        stale = call.get("prompt_sha256") != prompt_hash(system_prompt, user_prompt)
        self.stale += stale
        return (
            call["response"],
            call.get("finish_reason"),
            call.get("usage"),
            {"stale": stale, "recorded_latency_ms": call.get("latency_ms")},
        )


class LiveTransport(_Transport):
    """One plain completion per call with ``langchain_groq.ChatGroq``.

    The production tool path (GitNexus MCP ReAct agent with the 8B fallback)
    cannot report token usage per call, so live e2e runs use plain
    completions at a fixed temperature and seed; docs/EVALS.md lists this.
    """

    def __init__(self, temperature: float = 0.0, seed: int = 1234, chat_factory=None):
        super().__init__()
        self.temperature = temperature
        self.seed = seed
        self._chat_factory = chat_factory or _groq_chat
        self._chats: dict = {}

    async def _answer(self, role, system_prompt, user_prompt, model, api_key):
        from langchain_core.messages import HumanMessage, SystemMessage

        key = (model, api_key)
        if key not in self._chats:
            self._chats[key] = self._chat_factory(
                model=model,
                api_key=api_key,
                temperature=self.temperature,
                seed=self.seed,
            )
        message = await self._chats[key].ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        metadata = getattr(message, "response_metadata", None) or {}
        usage = getattr(message, "usage_metadata", None)
        text = message.content if isinstance(message.content, str) else ""
        return (
            text,
            metadata.get("finish_reason"),
            dict(usage) if usage else None,
            {},
        )


def _groq_chat(*, model, api_key, temperature, seed):  # pragma: no cover - network
    from langchain_groq import ChatGroq

    return ChatGroq(
        model=model,
        api_key=api_key,
        temperature=temperature,
        model_kwargs={"seed": seed},
        max_retries=2,
    )


# --- Scripted agents -------------------------------------------------------------


def _unexpected(role):
    raise AssertionError(f"the scripted agent has no answer for role {role!r}")


def oracle_pipeline(item: E2ETask) -> Policy:
    """PM approves, the Implementer writes the reference fix, the Maintainer
    approves: the best a model could do. Must resolve every task."""
    task = item.task
    fix = (task.solution_dir / task.target_file).read_text(encoding="utf-8")

    def policy(role, system, user):
        if role in ("pm", "maintainer"):
            return APPROVE
        if role == "implementer":
            return fix
        return _unexpected(role)

    return policy


def noop_pipeline(item: E2ETask) -> Policy:
    """PM approves, the Implementer returns the file unchanged, the Maintainer
    approves. Must resolve nothing."""
    task = item.task
    original = task.read_target()

    def policy(role, system, user):
        if role in ("pm", "maintainer"):
            return APPROVE
        if role == "implementer":
            return original
        return _unexpected(role)

    return policy


def oracle_reviewer(case: ReviewCase) -> Policy:
    """A Reviewer that always gives the right verdict."""

    def policy(role, system, user):
        if role != "maintainer":
            return _unexpected(role)
        return APPROVE if case.expected_verdict == "approve" else REJECT

    return policy


def noop_reviewer(case: ReviewCase) -> Policy:
    """A Reviewer that approves everything (a rubber stamp)."""

    def policy(role, system, user):
        if role != "maintainer":
            return _unexpected(role)
        return APPROVE

    return policy
