"""The engine context: how runtime helpers find the engine running them (#186).

An ``AutoMaintainerEngine`` publishes one ``EngineContext`` through the
``current_engine`` ContextVar for the duration of each run. Runtime helpers
that used to read a process global or the environment at call time
(``agents.get_supabase``, ``rate_limiter.get_rate_limit_manager``, the
GitHub App identity in ``github_app``, the workspace root, the verification,
merge-policy and change-limit settings) look here first and fall back to
their previous behaviour when no engine is active. ``run_agent_loop`` (the
API/Celery path) runs with no engine context, so it behaves exactly as
before.

A ContextVar rather than an argument threaded through every helper: graph
nodes, ``asyncio.to_thread`` workers and tasks created by a run all inherit
the run's context, so two engines in one event loop never see each other's
clients or settings.

Importing this module has no side effects and imports no runtime module.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from pydantic import SecretStr

from .config import AutoMaintainerConfig

__all__ = [
    "EngineContext",
    "current_engine",
    "active_engine",
    "active_config",
    "secret_value",
]


@dataclass(eq=False)
class EngineContext:
    """What a running engine shares with the helpers its run calls.

    ``supabase`` is the engine's client, or None for a run without a
    database (an engine never falls back to the process-wide client).
    ``llm`` and ``github`` are the optional injected transports (see
    ``AutoMaintainerEngine``). ``rate_limiter`` returns the engine's own
    ``RateLimitManager``, built on first use. ``slug_cache`` holds the
    engine's GitHub App slug, so two engines bound to different Apps never
    share one.
    """

    config: AutoMaintainerConfig
    engine_id: str
    rate_limiter: Callable[[], Any]
    supabase: Any = None
    llm: Optional[Callable[..., Any]] = None
    github: Optional[Callable[..., Any]] = None
    slug_cache: dict = field(default_factory=dict)


current_engine: ContextVar[Optional[EngineContext]] = ContextVar(
    "current_engine", default=None
)


def active_engine() -> Optional[EngineContext]:
    """The context of the engine running in this context, or None."""
    return current_engine.get()


def active_config() -> Optional[AutoMaintainerConfig]:
    """The configuration of the engine running in this context, or None."""
    ctx = current_engine.get()
    return ctx.config if ctx is not None else None


def secret_value(value: Optional[SecretStr]) -> Optional[str]:
    """The plain value of an optional ``SecretStr``."""
    return value.get_secret_value() if value is not None else None
