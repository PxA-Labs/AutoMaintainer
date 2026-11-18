"""The ADK Reviewer: a tool-using review inside the maintainer node (#525).

Selected with ``reviewer_backend="adk"`` (REVIEWER_BACKEND=adk without an
engine). It replaces only the single-shot review prompt: the maintainer node
still turns the decision into a comment and applies the trust gate and merge
policy exactly as with the LangGraph review. Design and rules:
docs/design/adk-specialist-agents.md.

One review, in ``review_pull_request``:

1. A ``ReviewRequest`` is built from the graph state and the PR, and the PR
   head is checked out at exactly its head SHA into a throwaway directory
   (``checkout_head``), deleted afterwards. The run's own clone is not used:
   it sits at the base branch and is shared with the run's other steps.
2. ``review`` builds a fresh ADK agent, runner and in-memory session (no
   module-level client, agent or session). The model is the engine's
   ``ModelRouting.maintainer`` model; a Groq model goes through ADK's LiteLLM
   wrapper with a key from the engine's rate limiter (its key pool and
   budget), a Gemini model through ADK's native client (Vertex AI with the
   process's Google credentials). The only tools are the four read-only ones
   of ``tools.ReviewTools``; each result reaches the model inside an
   ``<untrusted-data>`` block, as do the directive and diff in the request.
3. The answer is parsed with the same strict ``parse_decision`` into a
   ``MaintainerDecision``; an invalid answer gets the same single corrective
   retry (in the same session, under the same budgets).

Fail closed: exceeding ``ReviewBudget`` (tool calls, model calls, tokens,
time), any exception, a call to a tool that is not registered, a run stop
requested by ``engine.cancel()``, or an invalid answer after the retry each
return a rejection whose reason says why. Cancelling the run's task
(``engine.cancel(grace_seconds=...)``, ``aclose()``) is not swallowed: the
CancelledError propagates, so no decision at all is produced.

Accounting and tracing: the review's tokens are added to the run as one
TelemetryEvent, as the LangGraph review's are; the review is a
``specialist.reviewer`` span under the run's trace and every tool call a
``specialist.tool`` child span, both from the OpenTelemetry API's global
tracer (this package never imports the server's ``observability`` module).

ADK is imported inside functions only, so importing this module needs no
``adk`` extra.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .._runtime import agents
from .contracts import ReviewRequest
from .tools import ReviewTools, ToolRefused, ToolUnavailable

logger = logging.getLogger(__name__)

__all__ = [
    "ReviewBudget",
    "SYSTEM_PROMPT",
    "build_model",
    "checkout_head",
    "review",
    "review_pull_request",
]

AGENT_NAME = "reviewer"
APP_NAME = "automaintainer-reviewer"
TOOL_NAMES = ("get_diff", "read_file", "search_code", "get_verification_log")

SYSTEM_PROMPT = (
    "You are the Maintainer. Review the change for correctness, security and "
    "whether it resolves the originating task. Approve only if it is safe to "
    "merge; otherwise reject and point out the concrete flaw to fix. You have "
    "read-only tools over the pull request at its head commit: get_diff "
    "(the diff, whole or for one path, in pages), read_file (lines of a file "
    "at the head), search_code (a fixed-string search of the repository) and "
    "get_verification_log (this iteration's test output). Use them to check "
    "the changed code in context, at most 8 calls, then answer. You cannot "
    "change, comment on or merge anything. "
    + agents.MAINTAINER_VERIFICATION_INSTRUCTIONS
    + " "
    + agents.MAINTAINER_DECISION_INSTRUCTIONS
    + " "
    + agents.UNTRUSTED_DATA_NOTICE
)


@dataclass(frozen=True)
class ReviewBudget:
    """Limits of one review; hitting any of them is a rejection.

    ``max_tool_calls`` counts every call the model makes (refused ones too);
    ``max_model_calls`` leaves room for one answer after the last tool call
    plus the corrective retry; ``max_tokens`` is the sum of the provider's
    reported usage over the review (a model call whose response reports no
    usage counts an estimate of its prompt and answer instead, so a route
    without usage cannot disable the cap); ``max_output_tokens`` caps each
    answer; ``timeout_seconds`` covers the whole review, retry included.
    ``estimated_tokens`` is what the rate limiter reserves for the review
    when it hands out a key (the actual usage is not fed back to it).
    """

    max_tool_calls: int = 8
    max_model_calls: int = 11
    max_tokens: int = 60_000
    max_output_tokens: int = 4096
    timeout_seconds: float = 180.0
    estimated_tokens: int = 3000


class ReviewAborted(Exception):
    """A budget, a stop request or a protocol violation ended the review."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Usage:
    """Mutable counters of one review (never shared between reviews).

    ``abort`` records the first reason the review must end before raising
    it: ADK catches exceptions raised in callbacks and tools and reports
    them as an error event named after the exception class, so the reason
    is read back from here (``_check_event``). ``open_model_calls`` counts
    model calls that started and never returned (ADK swallows a cancelled
    model call without an event).
    """

    def __init__(self, budget: ReviewBudget):
        self.budget = budget
        self.tool_calls = 0
        self.model_calls = 0
        self.open_model_calls = 0
        self.tokens = 0
        self.aborted: Optional[str] = None

    def abort(self, reason: str):
        if self.aborted is None:
            self.aborted = reason
        raise ReviewAborted(self.aborted)

    def check_stop(self) -> None:
        if self.aborted is not None:
            raise ReviewAborted(self.aborted)
        should_stop = agents._run_should_stop.get()
        if should_stop is not None and should_stop():
            self.abort("the run was cancelled during the review")

    def tool_call(self) -> None:
        self.check_stop()
        self.tool_calls += 1
        if self.tool_calls > self.budget.max_tool_calls:
            self.abort(
                f"the review exceeded its budget of {self.budget.max_tool_calls} "
                "tool calls"
            )

    def model_call(self) -> None:
        self.check_stop()
        self.model_calls += 1
        if self.model_calls > self.budget.max_model_calls:
            self.abort(
                f"the review exceeded its budget of {self.budget.max_model_calls} "
                "model calls"
            )
        self.open_model_calls += 1

    def model_returned(self, tokens: int) -> None:
        self.open_model_calls -= 1
        self.tokens += max(int(tokens or 0), 0)
        if self.tokens > self.budget.max_tokens:
            self.abort(
                f"the review exceeded its token budget ({self.tokens} of "
                f"{self.budget.max_tokens} tokens)"
            )


def _reject(reason: str):
    reason = f"ADK review failed closed: {reason}; rejected."
    return agents.MaintainerDecision(
        verdict="reject", reasons=[reason], blocking_issues=[reason]
    )


def _task_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


# --- tracing ---------------------------------------------------------------------


@contextmanager
def _span(name: str, attributes: dict):
    """A span from the OpenTelemetry API's global tracer provider.

    The package never imports the server's ``observability`` module (the
    SDK wheel does not ship it): in the server, ``init_observability``
    installs the global provider, so these spans join the run's trace; an
    SDK host gets its own provider's spans, or the API's no-op tracer. No
    span at all where the OpenTelemetry API is not installed.
    """
    try:
        from opentelemetry import trace
    except ImportError:
        yield None
        return
    with trace.get_tracer("automaintainer.specialists").start_as_current_span(
        name, attributes=attributes
    ) as span:
        yield span


def _set(span, **attributes) -> None:
    if span is None:
        return
    for key, value in attributes.items():
        span.set_attribute(f"reviewer.{key}", value)


# --- model and credentials ----------------------------------------------------------


def _is_gemini(model_name: str) -> bool:
    name = model_name.split("/", 1)[1] if "/" in model_name else model_name
    return name.startswith("gemini") and (
        "/" not in model_name or model_name.startswith(("vertex_ai/", "gemini/"))
    )


def build_model(model_name: str, api_key: Optional[str]):
    """The ADK model for ``model_name`` (the routed Maintainer model).

    A Gemini name (``gemini-...``, ``vertex_ai/gemini-...``) uses ADK's
    native client: on Vertex AI with GOOGLE_GENAI_USE_VERTEXAI,
    GOOGLE_CLOUD_PROJECT and GOOGLE_CLOUD_LOCATION and the process's Google
    credentials (no key here). Anything else goes through LiteLLM, on Groq
    unless the name carries a provider prefix, with ``api_key``.
    """
    if _is_gemini(model_name):
        from google.adk.models.google_llm import Gemini

        return Gemini(model=model_name.split("/", 1)[-1])
    from google.adk.models.lite_llm import LiteLlm

    routed = model_name if "/" in model_name else f"groq/{model_name}"
    return LiteLlm(model=routed, api_key=api_key)


async def _acquire_key(model_name: str, estimated_tokens: int):
    """A key from the running engine's rate limiter (else the process one),
    reserving ``estimated_tokens``, or None for a model that takes no key.

    The limiter's own wait timeout is a rejection that says so, not a
    review timeout (the review's deadline cancels the wait instead, which
    the caller reports as the review timing out).
    """
    if _is_gemini(model_name):
        return None
    manager = await agents.get_rate_limit_manager()
    try:
        return await manager.acquire(estimated_tokens)
    except TimeoutError:
        raise ReviewAborted(
            "no model key was available within the rate limit"
        ) from None


# --- the review --------------------------------------------------------------------


def _request_prompt(request: ReviewRequest) -> str:
    return "\n\n".join(
        [
            f"Review pull request #{request.pr_number} of {request.repository} at "
            f"head {request.head_sha} (fix iteration {request.iteration}).",
            "Originating task:\n"
            + agents.untrusted_block("originating issue", request.directive),
            "Pull request diff against the base branch (use get_diff for pages "
            "past the cut):\n"
            + agents.untrusted_block(
                "pr diff", request.diff, max_chars=agents.MAX_REVIEW_DIFF_CHARS
            ),
            agents._verification_review_section(request.verification),
        ]
    )


def _adk_tools(tools: ReviewTools, usage: _Usage) -> list:
    """The four tools as async functions ADK can declare: each call counts
    against the budget, runs in a thread under its own span, and answers
    with untrusted data or a refusal."""

    async def call(name: str, label: str, fn) -> str:
        usage.tool_call()
        with _span("specialist.tool", {"tool.name": name}) as span:
            try:
                text = await asyncio.to_thread(fn)
                outcome = "ok"
            except ToolRefused as e:
                text, outcome = f"refused: {e}", "refused"
            except ToolUnavailable as e:
                text, outcome = f"unavailable: {e}", "unavailable"
            if span is not None:
                span.set_attribute("tool.outcome", outcome)
        return agents.untrusted_block(label, text)

    async def get_diff(path: Optional[str] = None, page: int = 1) -> str:
        """The pull request diff against its base branch, in pages.

        Args:
            path: a repository path to show only that file's changes; omit for
                the whole diff.
            page: the page to return, starting at 1.
        """
        return await call(
            "get_diff", f"get_diff {path or ''}", lambda: tools.get_diff(path, page)
        )

    async def read_file(path: str, start: int = 1, end: Optional[int] = None) -> str:
        """Numbered lines of a file at the pull request's head commit.

        Args:
            path: the file's path relative to the repository root.
            start: the first line, starting at 1.
            end: the last line (inclusive); omit to read to the end (capped).
        """
        return await call(
            "read_file", f"read_file {path}", lambda: tools.read_file(path, start, end)
        )

    async def search_code(query: str) -> str:
        """Lines of the repository at the head that contain ``query`` exactly.

        Args:
            query: the text to find (a fixed string, one line).
        """
        return await call(
            "search_code", "search_code", lambda: tools.search_code(query)
        )

    async def get_verification_log() -> str:
        """The end of the test output of this iteration's verification."""
        return await call(
            "get_verification_log",
            "verification log",
            tools.get_verification_log,
        )

    return [get_diff, read_file, search_code, get_verification_log]


def _check_event(event, usage: _Usage) -> None:
    """An event carrying an error ends the review.

    ADK reports an exception raised in a callback or tool as an error event
    named after its class (the reason is in ``usage.aborted``), seals a
    cancelled invocation with INVOCATION_ABORTED, and passes on the model
    provider's own error codes.
    """
    code = getattr(event, "error_code", None)
    if not code:
        return
    if usage.aborted is not None:
        raise ReviewAborted(usage.aborted)
    if code == "INVOCATION_ABORTED":
        usage.abort("the review was cancelled")
    safe = "".join(c for c in str(code) if c.isalnum() or c in "_-.")[:64]
    usage.abort(f"the model returned an error ({safe})")


def _estimate_tokens(chars: int) -> int:
    """About four characters per token, rounded up (at least one token)."""
    return max(1, -(-chars // 4))


def _content_chars(content) -> int:
    total = 0
    for part in (getattr(content, "parts", None) or []) if content else []:
        total += len(part.text or "")
        if part.function_call is not None:
            total += len(str(part.function_call.args or "")) + len(
                part.function_call.name or ""
            )
        if part.function_response is not None:
            total += len(str(part.function_response.response or ""))
    return total


def _request_chars(llm_request) -> int:
    """The size of what one model call sends: instruction and history."""
    system = getattr(llm_request.config, "system_instruction", None) or ""
    total = len(system) if isinstance(system, str) else _content_chars(system)
    return total + sum(_content_chars(c) for c in llm_request.contents or [])


def _final_text(events: list) -> str:
    texts = []
    for event in events:
        if getattr(event, "author", None) != AGENT_NAME:
            continue
        if not event.is_final_response() or event.content is None:
            continue
        for part in event.content.parts or []:
            if part.text and not getattr(part, "thought", False):
                texts.append(part.text)
    return "".join(texts)


async def _converse(request: ReviewRequest, tools: ReviewTools, usage: _Usage, model):
    from google.adk.agents import LlmAgent
    from google.adk.agents.run_config import RunConfig
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    prompt_chars = [0]

    def before_model(callback_context, llm_request):
        usage.model_call()
        prompt_chars[0] = _request_chars(llm_request)
        return None

    def after_model(callback_context, llm_response):
        meta = llm_response.usage_metadata
        tokens = 0
        if meta is not None:
            tokens = meta.total_token_count or (meta.prompt_token_count or 0) + (
                meta.candidates_token_count or 0
            )
        if not tokens:
            tokens = _estimate_tokens(
                prompt_chars[0] + _content_chars(llm_response.content)
            )
        usage.model_returned(tokens)
        content = llm_response.content
        for part in (content.parts if content is not None else None) or []:
            call = part.function_call
            if call is not None and call.name not in TOOL_NAMES:
                usage.abort("the model called a tool that does not exist")
        return None

    agent = LlmAgent(
        name=AGENT_NAME,
        model=model,
        # A callable, not a string: ADK would fill "{...}" in a string
        # instruction from session state, and the prompt contains JSON.
        instruction=lambda _context: SYSTEM_PROMPT,
        tools=_adk_tools(tools, usage),
        before_model_callback=before_model,
        after_model_callback=after_model,
        generate_content_config=types.GenerateContentConfig(
            max_output_tokens=usage.budget.max_output_tokens
        ),
        disallow_transfer_to_parent=True,
        disallow_transfer_to_peers=True,
    )
    sessions = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=sessions)
    user_id = "maintainer"
    session = await sessions.create_session(
        app_name=APP_NAME, user_id=user_id, session_id=uuid.uuid4().hex
    )
    run_config = RunConfig(max_llm_calls=usage.budget.max_model_calls + 1)

    async def ask(text: str) -> str:
        events = []
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text=text)]),
            run_config=run_config,
        ):
            _check_event(event, usage)
            events.append(event)
        if usage.aborted is not None:
            raise ReviewAborted(usage.aborted)
        if usage.open_model_calls:
            # A model call started and never returned: ADK swallowed its
            # cancellation and ended the invocation without an answer.
            usage.abort("the review was cancelled")
        # A stop requested during the last model call: no callback runs
        # after it, and its answer (perhaps an approval) must not be used.
        usage.check_stop()
        return _final_text(events)

    try:
        reply = await ask(_request_prompt(request))
        try:
            return agents.parse_decision(reply, agents.MaintainerDecision), None
        except agents.DecisionParseError as first:
            error = str(first)
        reply = await ask(agents.DECISION_RETRY_MESSAGE.format(error=error))
        try:
            return agents.parse_decision(reply, agents.MaintainerDecision), None
        except agents.DecisionParseError as second:
            error = str(second)
    finally:
        await runner.close()
    reason = (
        "The model's decision could not be parsed after one retry "
        f"({error}); rejected (fail closed)."
    )
    return (
        agents.MaintainerDecision(
            verdict="reject", reasons=[reason], blocking_issues=[reason]
        ),
        error,
    )


async def _emit(event) -> None:
    if agents.current_run_id.get(None):
        await agents.emit_event(event)


async def review(
    request: ReviewRequest,
    tools: ReviewTools,
    *,
    budget: ReviewBudget = ReviewBudget(),
):
    """Review ``request`` with ``tools``; ``(MaintainerDecision, parse_error)``.

    ``parse_error`` is set only when the answer was invalid twice, as for
    ``agents._llm_decision``. Never raises, except CancelledError when the
    calling task itself is cancelled.
    """
    from .._runtime.events import AgentPhase, ErrorEvent, TelemetryEvent

    model_name = agents._routed_model("maintainer")
    usage = _Usage(budget)
    started = time.monotonic()
    parse_error = None
    failure = None
    attributes = {
        "reviewer.backend": "adk",
        "reviewer.repository": request.repository,
        "reviewer.pr_number": request.pr_number,
        "reviewer.iteration": request.iteration,
        "reviewer.model": model_name,
    }
    with _span("specialist.reviewer", attributes) as span:
        key = None
        try:
            async with asyncio.timeout(budget.timeout_seconds):
                key = await _acquire_key(model_name, budget.estimated_tokens)
                model = build_model(model_name, getattr(key, "key", None))
                decision, parse_error = await _converse(request, tools, usage, model)
        except TimeoutError:
            failure = f"the review timed out after {budget.timeout_seconds:g}s"
        except ReviewAborted as e:
            failure = e.reason
        except asyncio.CancelledError:
            if _task_cancelling():
                _set(span, outcome="cancelled", tokens=usage.tokens)
                raise
            failure = "the review was cancelled"
        except Exception as e:  # fail closed on anything else
            logger.warning("ADK review failed: %s", type(e).__name__, exc_info=True)
            failure = f"the review raised {type(e).__name__}"
            if key is not None:
                key.record_error(e)
        else:
            if key is not None:
                key.record_success()
        if failure is not None:
            decision = _reject(failure)
        _set(
            span,
            outcome="failed" if failure else "decided",
            verdict=decision.verdict,
            tool_calls=usage.tool_calls,
            model_calls=usage.model_calls,
            tokens=usage.tokens,
        )
    run_id = agents.current_run_id.get(None)
    if run_id:
        await _emit(
            TelemetryEvent(
                run_id=run_id,
                phase=AgentPhase.MAINTAINER,
                latency_ms=int((time.monotonic() - started) * 1000),
                tokens_used=usage.tokens,
                model_name=model_name,
            )
        )
        if failure is not None:
            await _emit(
                ErrorEvent(
                    run_id=run_id,
                    phase=AgentPhase.MAINTAINER,
                    error_code="SPECIALIST_REVIEW_FAILED",
                    message=f"ADK review rejected (fail closed): {failure}",
                    fatal=False,
                )
            )
    return decision, parse_error


# --- the maintainer node's entry point ------------------------------------------------


class CheckoutError(RuntimeError):
    """The PR head could not be checked out at its head SHA."""


@asynccontextmanager
async def checkout_head(clone_url: str, branch: str, head_sha: str):
    """A shallow clone of ``branch`` at exactly ``head_sha``, deleted on exit.

    Cloned as verification.verify_pr_branch does: the run's token reaches git
    through its environment only (workspace.git_auth_env) and LFS objects
    are not fetched. A branch that moved since the head was read is an
    error, so the review never reads another commit than the one it judges.
    """
    from .._runtime import verification
    from .._runtime.workspace import git_auth_env

    if not isinstance(clone_url, str) or not clone_url:
        raise CheckoutError("the repository clone URL is unknown")
    token = await asyncio.to_thread(agents._run_github_token)
    workdir = tempfile.mkdtemp(prefix="automaintainer-review-")
    repo_dir = os.path.join(workdir, "repo")
    env = {**git_auth_env(token), "GIT_LFS_SKIP_SMUDGE": "1"}
    try:
        try:
            await verification._git(
                ["clone", "--quiet", "--depth", "1", "--single-branch", "--no-tags"]
                + ["--branch", branch, "--", clone_url, repo_dir],
                None,
                env,
                verification.CLONE_TIMEOUT_SECONDS,
            )
            cloned = await verification._git(["rev-parse", "HEAD"], repo_dir, env, 30)
        except (verification.RunnerError, OSError) as e:
            raise CheckoutError(f"git failed ({type(e).__name__})") from None
        if cloned != head_sha:
            raise CheckoutError("the branch moved since its head was read")
        yield Path(repo_dir)
    finally:
        await asyncio.to_thread(verification._rmtree, workdir)


def _verification_log(verification: Optional[dict]) -> str:
    if not verification:
        return ""
    return str(verification.get("output_tail") or verification.get("summary") or "")


def _diff_files(pr) -> list[tuple[str, str]]:
    return [
        (
            f.filename,
            f"--- {f.filename} ({f.status})\n{f.patch or agents._NO_PATCH_NOTE}",
        )
        for f in pr.get_files()
    ]


async def _head_retriever(repository_id, request: ReviewRequest):
    """``(retriever, top_k)`` for search_code: the run's RAG index when
    retrieval is on and it holds exactly the PR head commit, else None
    (ripgrep). Shares the PM's resolution (agents.resolve_retriever), so an
    engine's injected factory serves both. Never fails the review."""
    enabled, top_k = agents.rag_retrieval_settings()
    if not enabled or not repository_id:
        return None, top_k
    try:
        async with asyncio.timeout(agents.RAG_RETRIEVAL_TIMEOUT_SECONDS):
            retriever = await asyncio.to_thread(
                agents.resolve_retriever, repository_id, request.head_sha
            )
    except Exception as e:  # TimeoutError included
        logger.warning(
            "ADK review: the code index is unavailable (%s); search_code uses "
            "ripgrep",
            type(e).__name__,
        )
        return None, top_k
    return retriever, top_k


async def review_pull_request(state: dict, gh_repo: Any, pr: Any, diff_text):
    """The ADK review of the maintainer node; ``(decision, parse_error)``.

    ``diff_text`` is the diff the node already loaded. Without a pull
    request there is no head to review, which is a rejection; so is a head
    that cannot be checked out. Selecting this backend without the ``adk``
    extra (REVIEWER_BACKEND=adk; an engine's config refuses it at
    validation) ends the run: it never falls back to the other review.
    """
    from .. import config as config_module
    from .._runtime.events import AgentPhase

    missing = config_module._adk_missing()
    if missing is not None:
        raise agents.PipelineAbortError(
            "REVIEWER_BACKEND_UNAVAILABLE",
            f"The ADK reviewer is selected but {missing} is not installed; "
            "install automaintainer[adk] or set REVIEWER_BACKEND=langgraph.",
            AgentPhase.MAINTAINER,
        )
    if pr is None or diff_text is None:
        return _reject("there is no pull request head to review"), None
    try:
        request = ReviewRequest(
            repository=agents.normalize_repo_name(state["repo_name"]),
            pr_number=state.get("pr_number") or pr.number,
            head_sha=pr.head.sha,
            directive=state.get("idea") or state.get("architect_directive") or "",
            iteration=state.get("iteration", 0),
            verification=state.get("verification"),
            diff=diff_text,
        )
        files = await asyncio.to_thread(_diff_files, pr)
    except Exception as e:  # pydantic or GitHub: fail closed
        return _reject(f"the review could not be prepared ({type(e).__name__})"), None
    retriever, top_k = await _head_retriever(state.get("repository_id"), request)
    try:
        async with checkout_head(
            gh_repo.clone_url, pr.head.ref, request.head_sha
        ) as root:
            tools = ReviewTools(
                root,
                diff_files=files,
                verification_log=_verification_log(request.verification),
                retriever=retriever,
                search_k=top_k,
            )
            return await review(request, tools)
    except CheckoutError as e:
        return _reject(f"the pull request head could not be checked out: {e}"), None
    except Exception as e:  # mkdtemp, the token lookup, ...: fail closed
        logger.warning("ADK review checkout failed: %s", type(e).__name__)
        reason = f"the pull request head could not be checked out ({type(e).__name__})"
        return _reject(reason), None
