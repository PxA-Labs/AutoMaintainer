"""Typed contracts between the LangGraph engine and its specialists (#525).

The maintainer node sends a ``ReviewRequest`` and gets back the existing
``MaintainerDecision`` (agents.py), validated by the same pydantic model and
the same strict parser as the single-shot review. No free text crosses the
boundary.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ReviewRequest"]


class ReviewRequest(BaseModel):
    """What the Reviewer is asked to review.

    ``verification`` is the iteration's VerificationResult as a dict (None
    when nothing ran) and ``diff`` the PR diff the node fetched; the agent
    can page through the diff and read more through its tools. The
    directive, the diff and the verification output are written by whoever
    can open an issue or a PR, so the Reviewer treats them as data.
    """

    # hide_input_in_errors: a validation error never echoes untrusted text.
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    pr_number: int = Field(gt=0)
    # The head commit as the GitHub client reports it. Only the character set
    # is checked (a 40- or 64-hex id in production), so it cannot carry text
    # into the prompt it is quoted in.
    head_sha: str = Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z._-]{0,63}$")
    directive: str = ""
    iteration: int = Field(ge=0)
    verification: Optional[dict] = None
    diff: str = ""
