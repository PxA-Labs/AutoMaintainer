"""A fake ADK model for the Reviewer tests (#525): no network, no Groq.

``FakeModel`` is a real ``google.adk`` ``BaseLlm``, so the real ADK runner,
session, tool dispatch and callbacks run; only the model's answers are
scripted. ``policy(request, seen)`` returns, for each model turn, either the
final text or a list of ``(tool name, args)`` calls; ``seen`` is the list of
turns so far. Every turn is recorded as a ``Turn`` (system instruction, the
user texts and the tool results the model received).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from pydantic import Field

APPROVE = '{"verdict": "approve", "reasons": ["looks right"]}'
REJECT = (
    '{"verdict": "reject", "reasons": ["off by one"], '
    '"blocking_issues": ["x must be 2"], "requested_changes": ["set x = 2"]}'
)


@dataclass
class Turn:
    system: str
    user: list[str]
    tool_results: list[str]
    tools: list[str]
    api_key: Any = None
    model: str = ""

    @property
    def everything(self) -> str:
        return "\n".join(self.user + self.tool_results)


def calls(*pairs):
    """A model turn that calls tools: ``calls(("read_file", {"path": "a"}))``."""
    return list(pairs)


class FakeModel(BaseLlm):
    policy: Callable[..., Any]
    api_key: Any = None
    tokens_per_turn: int = 100
    delay: float = 0.0
    turns: list = Field(default_factory=list)

    async def generate_content_async(self, llm_request, stream=False):
        system = llm_request.config.system_instruction
        if not isinstance(system, str):
            system = "".join(getattr(p, "text", "") or "" for p in system.parts)
        user, results = [], []
        for content in llm_request.contents:
            for part in content.parts or []:
                if part.text and content.role == "user":
                    user.append(part.text)
                if part.function_response is not None:
                    results.append(str(part.function_response.response))
        tools = [
            d.name
            for t in (llm_request.config.tools or [])
            for d in (t.function_declarations or [])
        ]
        turn = Turn(system, user, results, tools, self.api_key, self.model)
        self.turns.append(turn)
        if self.delay:
            await asyncio.sleep(self.delay)
        answer = self.policy(turn, self.turns)
        if isinstance(answer, BaseException):
            raise answer
        if isinstance(answer, list):
            parts = [
                types.Part(function_call=types.FunctionCall(name=name, args=args))
                for name, args in answer
            ]
        else:
            parts = [types.Part(text=answer)]
        yield LlmResponse(
            content=types.Content(role="model", parts=parts),
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=self.tokens_per_turn - 10,
                candidates_token_count=10,
                total_token_count=self.tokens_per_turn,
            ),
        )


def scripted(*answers):
    """A policy answering the given turns in order (the last one repeats)."""

    def policy(turn, turns):
        return answers[min(len(turns), len(answers)) - 1]

    return policy


@dataclass
class ModelFactory:
    """Stands in for ``reviewer_adk.build_model``: one FakeModel per review,
    built with the model name and API key the reviewer resolved."""

    policy: Callable[..., Any]
    delay: float = 0.0
    tokens_per_turn: int = 100
    built: list = field(default_factory=list)

    def __call__(self, model_name, api_key):
        model = FakeModel(
            model=model_name,
            policy=self.policy,
            api_key=api_key,
            delay=self.delay,
            tokens_per_turn=self.tokens_per_turn,
        )
        self.built.append(model)
        return model

    @property
    def turns(self) -> list:
        return [t for m in self.built for t in m.turns]
