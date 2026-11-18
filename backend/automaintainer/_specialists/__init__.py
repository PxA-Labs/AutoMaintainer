"""Specialist agents called from the LangGraph engine (#525).

LangGraph stays the orchestrator and keeps every authority: run state,
cancellation, the retry loop, the trust gate, merge policy and all writes to
GitHub. A specialist is called from one graph node with a typed request and
returns a typed result; it only reads. See docs/design/adk-specialist-agents.md.

- ``contracts``: the typed request (the result is the existing
  ``MaintainerDecision``).
- ``tools``: the read-only tools, plain Python with no ADK dependency.
- ``reviewer_adk``: the Reviewer built with Google's Agent Development Kit,
  selected with ``reviewer_backend="adk"``. ADK is imported inside its
  functions only, so importing this package never needs the ``adk`` extra.

Importing this package has no side effects and imports no runtime module.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = ["uses_adk_reviewer"]


def uses_adk_reviewer(backend: Optional[str]) -> bool:
    """Whether ``backend`` (``reviewer_backend`` or REVIEWER_BACKEND) is ``adk``.

    An engine's value is already validated. A process without an engine
    reads REVIEWER_BACKEND; like the runtime's other variables, an unknown
    value is logged and the default (``langgraph``) kept.
    """
    value = (backend or "").strip().lower()
    if value == "adk":
        return True
    if value not in ("", "langgraph"):
        logger.warning(
            "Unknown REVIEWER_BACKEND %r; using the langgraph reviewer", backend
        )
    return False
