"""Model access for the harness: live Groq calls and recorded cassettes.

A cassette is ``<cassettes_dir>/<task_id>.json``::

    {"task_id", "prompt_sha256", "prompt_version", "model", "temperature",
     "seed", "response", "finish_reason", "usage", "latency_ms",
     "synthetic", "note"}

``prompt_sha256`` binds the recorded output to the exact prompt it answered.
Replay still runs when the current prompt differs, but marks the task as
``stale_cassette`` so nobody mistakes the numbers for the new prompt's.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CASSETTES_DIR = Path(__file__).resolve().parent / "cassettes" / "synthetic"
DEFAULT_MODEL = "llama-3.3-70b-versatile"  # the Implementer's primary model


@dataclass
class Completion:
    text: str
    finish_reason: str | None = None
    usage: dict | None = None  # input_tokens / output_tokens / total_tokens
    latency_ms: int | None = None
    meta: dict = field(default_factory=dict)


# (system_prompt, user_prompt) -> Completion
Completer = Callable[[str, str], Completion]


def groq_completer(
    api_key: str, model: str, temperature: float, seed: int
) -> Completer:
    """A plain (tool-free) completion with the project's LLM client.

    The production Implementer goes through ``run_llm_with_tools`` (a GitNexus
    MCP ReAct agent, provider-default temperature). The eval deliberately uses
    a single completion at fixed temperature and seed so runs are repeatable
    and need no MCP server; docs/EVALS.md lists this as a limitation.
    """
    from langchain_core.messages import HumanMessage, SystemMessage
    from langchain_groq import ChatGroq

    llm = ChatGroq(
        model=model,
        api_key=api_key,
        temperature=temperature,
        model_kwargs={"seed": seed},
        max_retries=2,
    )

    def complete(system_prompt: str, user_prompt: str) -> Completion:
        started = time.perf_counter()
        message = llm.invoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
        metadata = getattr(message, "response_metadata", None) or {}
        usage = getattr(message, "usage_metadata", None)
        return Completion(
            text=message.content if isinstance(message.content, str) else "",
            finish_reason=metadata.get("finish_reason"),
            usage=dict(usage) if usage else None,
            latency_ms=latency_ms,
            meta={"system_fingerprint": metadata.get("system_fingerprint")},
        )

    return complete


def load_cassette(cassettes_dir: Path, task_id: str) -> dict:
    path = cassettes_dir / f"{task_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"no cassette for task {task_id!r} at {path}")


def save_cassette(
    cassettes_dir: Path,
    task_id: str,
    completion: Completion,
    *,
    prompt_sha256: str,
    prompt_version: str,
    model: str,
    temperature: float,
    seed: int,
) -> None:
    cassettes_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "task_id": task_id,
        "prompt_sha256": prompt_sha256,
        "prompt_version": prompt_version,
        "model": model,
        "temperature": temperature,
        "seed": seed,
        "synthetic": False,
        "note": "recorded with python -m evals.run --mode live --record",
        "response": completion.text,
        "finish_reason": completion.finish_reason,
        "usage": completion.usage,
        "latency_ms": completion.latency_ms,
    }
    (cassettes_dir / f"{task_id}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def cassette_completion(record: dict) -> Completion:
    return Completion(
        text=record["response"],
        finish_reason=record.get("finish_reason"),
        usage=record.get("usage"),
        latency_ms=record.get("latency_ms"),
    )
