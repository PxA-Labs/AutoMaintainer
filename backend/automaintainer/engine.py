"""AutoMaintainerEngine: an instantiable agent pipeline (#186, epic #157).

Several engines with different configurations can run in one process and one
event loop without sharing clients, settings or caches:

- Each engine publishes one ``EngineContext`` (automaintainer.context) for
  the duration of each of its runs. The runtime helpers the graph nodes call
  (Supabase, the LLM and its rate limiter, GitHub credentials and the App
  slug, the workspace root, verification, merge policy and change limits)
  resolve against it.
- Clients are per instance and built lazily: the rate limiter (key pool and
  budgets from the config) on the first LLM call, the compiled graph on the
  first run. The constructor does no I/O.
- ``run()`` executes the graph in an asyncio task owned by the engine and
  binds the run's ContextVars with tokens that are reset when it ends,
  whether it completes, fails or is cancelled.

``run_agent_loop`` (the API and Celery path) runs on the process-default
engine, which has no ``AutoMaintainerConfig`` and publishes no engine
context: it reads the environment and the module-level clients exactly as
before. It is deliberately not built from ``AutoMaintainerConfig.from_env()``:
that validation is stricter than the runtime (it refuses a missing Groq key
or a malformed value the runtime logs and ignores) and it snapshots the
environment, so it would change when and how such a run fails.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import uuid
import weakref
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import agents
from rate_limiter import RateLimitManager

from .config import AutoMaintainerConfig
from .context import EngineContext, current_engine

__all__ = [
    "AutoMaintainerEngine",
    "RunResult",
    "cancel_active_run",
    "default_engine",
]

# An injected LLM transport: one completion with a key from the engine's
# pool. Called as llm(system_prompt=, user_prompt=, model=, api_key=).
LLMTransport = Callable[..., Awaitable[str]]
# An injected GitHub credentials factory, called like
# github_app.credentials_for_installation(installation_id, purpose=,
# repository=); returns an object with .mode, .token() and .client().
CredentialsFactory = Callable[..., Any]

_engine_ids = itertools.count(1)
# Every live engine, so a cancel request can find a run in this process.
_engines: "weakref.WeakSet[AutoMaintainerEngine]" = weakref.WeakSet()


@dataclass(frozen=True)
class RunResult:
    """The outcome of ``AutoMaintainerEngine.run``.

    ``status`` is ``completed``, ``failed`` or ``cancelled`` (the terminal
    statuses of tasks.TERMINAL_RUN_STATUSES). ``raw`` is the dictionary
    run_agent_loop returns for the same run.
    """

    status: str
    run_id: str
    pr_url: Optional[str] = None
    error_code: Optional[str] = None
    error: Optional[str] = None
    raw: Optional[dict] = None

    @classmethod
    def from_loop(cls, raw: dict, run_id: str, repo_name: str) -> "RunResult":
        pr_number = raw.get("pr_number") or 0
        repo = agents.normalize_repo_name(repo_name)
        pr_url = (
            f"https://github.com/{repo}/pull/{pr_number}"
            if pr_number and repo
            else None
        )
        return cls(
            status=raw.get("status", "failed"),
            run_id=run_id,
            pr_url=pr_url,
            error_code=raw.get("error_code"),
            error=raw.get("error"),
            raw=raw,
        )


class _ActiveRun:
    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.task: Optional[asyncio.Task] = None
        # threading.Event: a cancel may come from another thread (Celery's
        # cancel task runs on its own event loop).
        self.stop = threading.Event()
        self.progress: dict = {}


class AutoMaintainerEngine:
    """One configured instance of the agent pipeline.

    ``supabase``: the engine's database client, or None to run without one
    (the engine never uses the process-wide client). ``llm``: an optional
    LLM transport replacing the Groq call (rate limiting and key rotation
    still apply); without it the engine calls Groq with its own key pool.
    ``github``: an optional credentials factory replacing the GitHub App /
    PAT credentials derived from the config. ``emitter``: the default event
    emitter of the engine's runs (else the Supabase emitter, else none).
    ``nodes``: replacement node functions, as ``agents.build_graph``.

    Use it as an async context manager, or call ``aclose()``.
    """

    def __init__(
        self,
        config: AutoMaintainerConfig,
        *,
        supabase: Any = None,
        llm: Optional[LLMTransport] = None,
        github: Optional[CredentialsFactory] = None,
        emitter: Any = None,
        nodes: Optional[dict] = None,
    ):
        if not isinstance(config, AutoMaintainerConfig):
            raise TypeError("config must be an AutoMaintainerConfig")
        self._init_common(emitter, nodes)
        self.config = config
        self._context: Optional[EngineContext] = EngineContext(
            config=config,
            engine_id=self.engine_id,
            rate_limiter=self.rate_limiter,
            supabase=supabase,
            llm=llm,
            github=github,
        )

    def _init_common(self, emitter: Any, nodes: Optional[dict]) -> None:
        self.engine_id = f"engine-{next(_engine_ids)}"
        self._emitter = emitter
        self._nodes = dict(nodes) if nodes else None
        self._graph = None
        self._rate_limiter: Optional[RateLimitManager] = None
        self._lock = threading.Lock()
        self._runs: dict[str, _ActiveRun] = {}
        self._closed = False
        _engines.add(self)

    # --- Per-instance clients -------------------------------------------------

    def rate_limiter(self) -> RateLimitManager:
        """The engine's own rate limiter (key pool and budgets from the
        config), created on first use."""
        with self._lock:
            if self._rate_limiter is None:
                config = self.config
                self._rate_limiter = RateLimitManager(
                    [k.get_secret_value() for k in config.groq_api_keys],
                    requests_per_minute=config.groq_requests_per_minute,
                    tokens_per_minute=config.groq_tokens_per_minute,
                )
            return self._rate_limiter

    def graph(self):
        """The engine's compiled graph, built on first use."""
        with self._lock:
            if self._graph is None:
                self._graph = agents.build_graph(self._nodes)
            return self._graph

    # --- Lifecycle ------------------------------------------------------------

    async def aclose(self) -> None:
        """Cancel the engine's runs in progress and release its clients."""
        self._closed = True
        tasks = [r.task for r in list(self._runs.values()) if r.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._rate_limiter is not None:
            await self._rate_limiter.shutdown()
            self._rate_limiter = None

    async def __aenter__(self) -> "AutoMaintainerEngine":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # --- Runs -----------------------------------------------------------------

    async def run(
        self,
        repo_name: str,
        *,
        target_issue: Optional[int] = None,
        run_id: Optional[str] = None,
        trigger: Optional[dict] = None,
        repository_id: Optional[int] = None,
        installation_id: Optional[int] = None,
        emitter: Any = None,
    ) -> RunResult:
        """Run the pipeline on ``repo_name`` and return its outcome.

        ``trigger`` is {"source", "login", "github_id"} of whoever caused the
        run (the merge gate checks it, see run_agent_loop). A run stopped by
        ``cancel`` returns status ``cancelled``; cancelling the task that
        awaits ``run`` raises CancelledError as usual.
        """
        run_id = run_id or str(uuid.uuid4())
        raw = await self._run_loop(
            repo_name,
            target_issue=target_issue,
            run_id=run_id,
            emitter=emitter,
            installation_id=installation_id,
            repository_id=repository_id,
            trigger=trigger,
        )
        return RunResult.from_loop(raw, run_id, repo_name)

    def cancel(self, run_id: str, *, grace_seconds: Optional[float] = None) -> bool:
        """Ask the run ``run_id`` of this engine to stop; False if unknown.

        The run stops at the next node boundary: the node in flight (a git
        push, a test run) finishes, the next one never starts. With
        ``grace_seconds`` the run's task is also cancelled when it has not
        stopped by then (0: at once), interrupting the node at its next
        await. Either way the run ends ``cancelled`` with RUN_CANCELLED.
        Thread-safe.
        """
        active = self._runs.get(run_id)
        if active is None:
            return False
        active.stop.set()
        if grace_seconds is not None and active.task is not None:
            task = active.task

            def _cancel_later():
                active.loop.call_later(
                    max(grace_seconds, 0), lambda: task.done() or task.cancel()
                )

            active.loop.call_soon_threadsafe(_cancel_later)
        return True

    async def _run_loop(
        self,
        repo_name: str,
        *,
        target_issue: Optional[int],
        run_id: Optional[str],
        emitter: Any,
        installation_id: Optional[int],
        repository_id: Optional[int],
        trigger: Optional[dict],
    ) -> dict:
        """The run as run_agent_loop's result dictionary."""
        if self._closed:
            raise RuntimeError("AutoMaintainerEngine is closed")
        run_id = run_id or str(uuid.uuid4())
        if run_id in self._runs:
            raise RuntimeError(f"Run {run_id} is already running on this engine")
        trigger = dict(trigger or {})
        trigger = {
            "source": trigger.get("source"),
            "login": trigger.get("login"),
            "github_id": trigger.get("github_id"),
        }
        active = _ActiveRun(asyncio.get_running_loop())
        self._runs[run_id] = active
        try:
            # The run's own task: create_task copies the caller's context, so
            # nothing the run binds can leak into it, and cancel() can stop
            # the run without cancelling the caller.
            active.task = asyncio.create_task(
                self._execute(
                    repo_name,
                    run_id=run_id,
                    target_issue=target_issue,
                    emitter=emitter if emitter is not None else self._emitter,
                    installation_id=installation_id,
                    repository_id=repository_id,
                    trigger=trigger,
                    active=active,
                ),
                name=f"automaintainer-run-{run_id}",
            )
            try:
                return await active.task
            except asyncio.CancelledError:
                current = asyncio.current_task()
                caller_cancelled = current is not None and current.cancelling() > 0
                if active.stop.is_set() and not caller_cancelled:
                    # Cancelled through cancel(): the run already emitted
                    # RUN_CANCELLED and marked itself cancelled.
                    return agents.cancelled_result(active.progress.get("final_state"))
                raise
        finally:
            self._runs.pop(run_id, None)

    def _graph_for_run(self):
        return self.graph()

    async def _execute(
        self, repo_name: str, *, run_id: str, active: _ActiveRun, **kwargs
    ) -> dict:
        engine_token = current_engine.set(self._context)
        run_token = agents.current_run_id.set(run_id)
        trust_token = agents._run_trigger_trust.set({})
        creds_token = agents._run_github_credentials.set(None)
        emitter_token = agents.current_emitter.set(None)
        try:
            return await agents._run_pipeline(
                self._graph_for_run(),
                repo_name,
                run_id=run_id,
                target_issue=kwargs["target_issue"],
                emitter=kwargs["emitter"],
                github_installation_id=kwargs["installation_id"],
                repository_id=kwargs["repository_id"],
                trigger=kwargs["trigger"],
                should_stop=active.stop.is_set,
                progress=active.progress,
            )
        finally:
            agents.current_emitter.reset(emitter_token)
            agents._run_github_credentials.reset(creds_token)
            agents._run_trigger_trust.reset(trust_token)
            agents.current_run_id.reset(run_token)
            current_engine.reset(engine_token)


class _ProcessDefaultEngine(AutoMaintainerEngine):
    """The engine behind run_agent_loop: no config and no engine context, so
    every helper keeps reading the environment and the module-level clients;
    the graph is agents.get_default_graph() (``agents.app``)."""

    def __init__(self):
        self._init_common(None, None)
        self.config = None
        self._context = None

    def rate_limiter(self) -> RateLimitManager:  # pragma: no cover - unused
        raise RuntimeError("the default engine uses the process rate limiter")

    def _graph_for_run(self):
        return agents.get_default_graph()

    async def aclose(self) -> None:
        # Shared by the whole process: cancel nothing, close nothing.
        return None


_default_engine: Optional[_ProcessDefaultEngine] = None
_default_engine_lock = threading.Lock()


def default_engine() -> AutoMaintainerEngine:
    """The process-default engine that run_agent_loop runs on."""
    global _default_engine
    with _default_engine_lock:
        if _default_engine is None:
            _default_engine = _ProcessDefaultEngine()
        return _default_engine


def cancel_active_run(run_id: str, *, grace_seconds: Optional[float] = None) -> bool:
    """Cancel ``run_id`` on whichever engine of this process runs it (see
    ``AutoMaintainerEngine.cancel``); False when none does."""
    return any(
        engine.cancel(run_id, grace_seconds=grace_seconds) for engine in list(_engines)
    )
