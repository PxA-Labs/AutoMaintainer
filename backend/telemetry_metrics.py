"""Turn the engine's TelemetryEvents into OpenTelemetry metrics (server side).

The engine (backend/automaintainer, shipped as the SDK wheel) reports every
LLM call as a ``TelemetryEvent`` through its event emitter and never imports
``observability``: SDK users bring their own telemetry. The server wraps the
emitter of each run it starts (tasks.py) in ``MetricsEmitter``, which records

- ``llm_tokens_total`` and ``llm_phase_tokens_total``: the event's model,
  its ``phase`` (the agent phase that made the call) and kind ``total``,
  because the engine reports one count per call, not split into prompt and
  completion tokens;
- ``llm_estimated_cost_usd_total``: those tokens priced at the model's
  higher rate (``observability.estimated_llm_cost_upper_bound_usd``), an
  upper bound, so the cost alert errs towards firing early. The event's own
  ``cost_usd`` is not used: the engine always leaves it at 0.

and then forwards every event, unchanged, to the emitter the run would have
used anyway (the Supabase one). Recording never raises into the run.
"""

from __future__ import annotations

import logging
from typing import Optional

from automaintainer._runtime.events import (
    BaseEvent,
    EventEmitter,
    SupabaseEmitter,
    TelemetryEvent,
)
from observability import (
    estimated_llm_cost_upper_bound_usd,
    record_llm_estimated_cost,
    record_llm_tokens,
)

logger = logging.getLogger(__name__)


def record_telemetry_event(event: TelemetryEvent) -> None:
    """Record the token count and estimated cost of one LLM call."""
    tokens = event.tokens_used
    if not tokens or tokens < 0:
        return
    phase = getattr(event.phase, "value", event.phase)
    model = event.model_name
    record_llm_tokens(model, tokens, phase=phase, kind="total")
    cost = estimated_llm_cost_upper_bound_usd(model, tokens)
    if cost is not None:
        record_llm_estimated_cost(model, cost, phase)


class MetricsEmitter(EventEmitter):
    """Records TelemetryEvents as metrics, then forwards every event.

    ``inner`` defaults to a ``SupabaseEmitter`` without a client, which
    resolves the process's Supabase client at each emit, as the engine's
    own default emitter does (and drops the event when there is none).
    """

    def __init__(self, inner: Optional[EventEmitter] = None):
        self.inner = inner if inner is not None else SupabaseEmitter()

    async def emit(self, event: BaseEvent) -> None:
        if isinstance(event, TelemetryEvent):
            try:
                record_telemetry_event(event)
            except Exception as e:  # metrics must never break a run
                logger.warning(f"Could not record LLM metrics: {e!r}")
        await self.inner.emit(event)

    async def close(self) -> None:
        await self.inner.close()
