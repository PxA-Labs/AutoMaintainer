"""
Rate Limit Manager for AutoMaintainer
Provides token bucket rate limiting with multi-key rotation for Groq API.

Event-loop independence: Celery tasks each run on a fresh event loop
(asyncio.run / new_event_loop), while the manager is a process-wide
singleton. So nothing here may depend on a particular loop: key health is
derived lazily from timestamps at selection time (no background recovery
task) and the internal lock is a threading.Lock guarding purely
synchronous critical sections.
"""

import os
import re
import time
import asyncio
import logging
from typing import Optional, List, Dict, Any, Sequence
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
import threading

from automaintainer.context import active_engine

logger = logging.getLogger(__name__)

# How long a key is benched after repeated key-attributable errors.
UNHEALTHY_COOLDOWN_SECONDS = 120.0
UNHEALTHY_AFTER_CONSECUTIVE_ERRORS = 3

_STATUS_IN_MESSAGE = re.compile(
    r"(?:error code|status(?: code)?)\s*[:=]?\s*(\d{3})", re.I
)


class RequestExceedsBudgetError(ValueError):
    """A single request needs more tokens than any key's per-minute budget."""


def classify_key_error(error: Exception) -> Optional[str]:
    """
    Decide whether an error says something about the API key itself.

    Returns "rate_limit", "auth" or "server" for errors that should count
    against the key's health, or None for everything else (bad prompts,
    context-length errors, tool/MCP failures, parsing bugs...), which would
    fail identically on any key and must not bench healthy keys.
    """
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    msg = str(error).lower()
    if not isinstance(status, int):
        match = _STATUS_IN_MESSAGE.search(msg)
        status = int(match.group(1)) if match else None

    if (
        status == 429
        or "rate limit" in msg
        or "rate_limit" in msg
        or "too many requests" in msg
    ):
        return "rate_limit"
    if status in (401, 403) or "invalid api key" in msg or "invalid_api_key" in msg:
        return "auth"
    if (isinstance(status, int) and status >= 500) or type(error).__name__ in (
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
    ):
        return "server"
    return None


@dataclass
class APIKeyState:
    """Track state of a single API key."""

    key: str
    name: str
    requests_per_minute: int = 30  # Groq free tier default
    tokens_per_minute: int = 6000

    # Token bucket state
    request_tokens: float = field(default=30.0)
    token_tokens: float = field(default=6000.0)
    last_refill: float = field(default_factory=time.time)

    # Health tracking. A key is unhealthy while time.time() < unhealthy_until;
    # it recovers on its own once that passes, whichever loop/task asks.
    consecutive_errors: int = 0
    last_error_time: float = 0
    unhealthy_until: float = 0.0
    total_requests: int = 0
    total_tokens: int = 0

    @property
    def is_healthy(self) -> bool:
        if self.unhealthy_until and time.time() >= self.unhealthy_until:
            # Cooldown elapsed: give the key a fresh start.
            self.unhealthy_until = 0.0
            self.consecutive_errors = 0
            logger.info(f"Key {self.name} auto-recovered after cooldown")
        return not self.unhealthy_until

    def refill(self, now: float = None):
        """Refill token buckets based on elapsed time."""
        now = now or time.time()
        elapsed = now - self.last_refill
        if elapsed > 0:
            # Refill requests (per minute)
            self.request_tokens = min(
                self.requests_per_minute,
                self.request_tokens + (self.requests_per_minute * elapsed / 60.0),
            )
            # Refill tokens (per minute)
            self.token_tokens = min(
                self.tokens_per_minute,
                self.token_tokens + (self.tokens_per_minute * elapsed / 60.0),
            )
            self.last_refill = now

    def can_make_request(self, estimated_tokens: int = 1000) -> bool:
        """Check if we have capacity for a request."""
        self.refill()
        return (
            self.request_tokens >= 1
            and self.token_tokens >= estimated_tokens
            and self.is_healthy
        )

    def consume(self, tokens_used: int = 0):
        """Consume tokens for a request."""
        self.refill()
        self.request_tokens -= 1
        if tokens_used > 0:
            self.token_tokens -= tokens_used
        self.total_requests += 1
        self.total_tokens += max(tokens_used, 0)

    def record_error(self, error: Exception) -> bool:
        """
        Record an error against this key and potentially mark it unhealthy.

        Returns True if the error was attributable to the key (rate limit,
        auth or server error) and was counted, False if it was ignored.
        """
        kind = classify_key_error(error)
        if kind is None:
            return False

        now = time.time()
        self.consecutive_errors += 1
        self.last_error_time = now

        if kind == "rate_limit":
            # Drain request bucket on rate limit
            self.request_tokens = 0
            logger.warning(
                f"Rate limit hit for key {self.name}, draining request bucket"
            )

        if self.consecutive_errors >= UNHEALTHY_AFTER_CONSECUTIVE_ERRORS:
            self.unhealthy_until = now + UNHEALTHY_COOLDOWN_SECONDS
            logger.error(
                f"Key {self.name} marked unhealthy for {UNHEALTHY_COOLDOWN_SECONDS:.0f}s "
                f"after {self.consecutive_errors} {kind} errors"
            )
        return True

    def record_success(self):
        """Record successful request."""
        self.consecutive_errors = 0
        if self.unhealthy_until:
            self.unhealthy_until = 0.0
            logger.info(f"Key {self.name} recovered, marked healthy")

    def time_until_ready(self, estimated_tokens: int = 1000) -> float:
        """Estimate seconds until key can handle a request."""
        self.refill()
        if self.can_make_request(estimated_tokens):
            return 0.0
        if not self.is_healthy:
            return max(self.unhealthy_until - time.time(), 0.0)

        # Time to get 1 request token
        req_wait = 0
        if self.request_tokens < 1:
            req_wait = (1 - self.request_tokens) * 60.0 / self.requests_per_minute

        # Time to get enough token tokens
        token_wait = 0
        if self.token_tokens < estimated_tokens:
            token_wait = (
                (estimated_tokens - self.token_tokens) * 60.0 / self.tokens_per_minute
            )

        return max(req_wait, token_wait)


class RateLimitManager:
    """
    Manages multiple Groq API keys with token bucket rate limiting
    and automatic key rotation.
    """

    def __init__(
        self,
        keys: Optional[Sequence[str]] = None,
        *,
        requests_per_minute: Optional[int] = None,
        tokens_per_minute: Optional[int] = None,
    ):
        """``keys`` and the per-key budgets default to the environment
        (GROQ_API_KEY..., GROQ_REQUESTS_PER_MINUTE, GROQ_TOKENS_PER_MINUTE),
        read here and in ``initialize``; an engine passes its own (#186)."""
        self._configured_keys = tuple(keys) if keys is not None else None
        self.keys: List[APIKeyState] = []
        self.current_key_index: int = 0
        # threading.Lock, not asyncio.Lock: the critical sections below never
        # await, and an asyncio.Lock can end up bound to an event loop that a
        # previous Celery task already closed.
        self._lock = threading.Lock()
        self._initialized = False

        # Configuration
        self.min_healthy_keys = 1
        if requests_per_minute is None:
            requests_per_minute = int(os.getenv("GROQ_REQUESTS_PER_MINUTE", "30"))
        if tokens_per_minute is None:
            tokens_per_minute = int(os.getenv("GROQ_TOKENS_PER_MINUTE", "6000"))
        self.default_request_rpm = requests_per_minute
        self.default_token_rpm = tokens_per_minute

    async def initialize(self):
        """Load the API keys given to the constructor, else from environment."""
        if self._initialized:
            return

        keys = []
        if self._configured_keys is not None:
            # Same names as the environment pool: GROQ_API_KEY is "primary".
            names = ["primary"] + [f"key_{i}" for i in range(1, 20)]
            keys = list(zip(names, self._configured_keys))
        else:
            primary = os.getenv("GROQ_API_KEY")
            if primary:
                keys.append(("primary", primary))

            for i in range(1, 20):
                k = os.getenv(f"GROQ_API_KEY_{i}")
                if k:
                    keys.append((f"key_{i}", k))

        if not keys:
            raise ValueError("No GROQ_API_KEY found in environment")

        self.keys = [
            APIKeyState(
                key=key,
                name=name,
                requests_per_minute=self.default_request_rpm,
                tokens_per_minute=self.default_token_rpm,
            )
            for name, key in keys
        ]

        logger.info(f"RateLimitManager initialized with {len(self.keys)} API keys")
        self._initialized = True
        # No background recovery task: it would be bound to the event loop of
        # whichever task initialized us and die with it. APIKeyState.is_healthy
        # recovers keys lazily from their unhealthy_until timestamp instead.

    async def shutdown(self):
        """Kept for API compatibility; there are no background tasks to stop."""
        return None

    def _get_next_healthy_key(
        self, estimated_tokens: int = 1000
    ) -> Optional[APIKeyState]:
        """Find the next healthy key that can handle the request."""
        if not self.keys:
            return None

        # Try current key first
        for _ in range(len(self.keys)):
            key = self.keys[self.current_key_index]
            if key.is_healthy and key.can_make_request(estimated_tokens):
                return key

            # Move to next key
            self.current_key_index = (self.current_key_index + 1) % len(self.keys)

        return None

    async def acquire(
        self, estimated_tokens: int = 1000, timeout: float = 60.0
    ) -> APIKeyState:
        """
        Acquire an API key for making a request.
        Waits if all keys are rate limited.
        """
        if not self._initialized:
            await self.initialize()

        # A bucket never holds more than tokens_per_minute, so a bigger
        # request can never be served: fail now rather than wait out the
        # whole timeout.
        budget = max((k.tokens_per_minute for k in self.keys), default=0)
        if estimated_tokens > budget:
            raise RequestExceedsBudgetError(
                f"Request needs ~{estimated_tokens} tokens but the per-minute "
                f"budget is {budget} (GROQ_TOKENS_PER_MINUTE); shrink the "
                f"request or raise the budget"
            )

        start_time = time.time()

        while True:
            with self._lock:
                key = self._get_next_healthy_key(estimated_tokens)
                if key:
                    key.consume(estimated_tokens)
                    return key

                # All keys busy - wait for the fastest one
                wait_times = [
                    (k.time_until_ready(estimated_tokens), k)
                    for k in self.keys
                    if k.is_healthy
                ]

            if not wait_times:
                # No healthy keys at all (they recover after their cooldown)
                raise RuntimeError("No healthy API keys available")

            min_wait, _ = min(wait_times, key=lambda x: x[0])
            wait_time = min(min_wait, 5.0)  # Cap at 5 seconds per check

            if time.time() - start_time + wait_time > timeout:
                raise TimeoutError(f"Rate limit timeout after {timeout}s")

            await asyncio.sleep(wait_time)

    async def execute_with_retry(
        self, func, *args, estimated_tokens: int = 1000, max_retries: int = 3, **kwargs
    ) -> Any:
        """
        Execute a function with automatic rate limit handling and key rotation.

        The acquired API key is ALWAYS injected as the ``api_key`` keyword
        argument, so wrapped callables must accept it (e.g. ``def f(api_key: str)``).
        This guarantees the key that was rate-limit-checked is exactly the key
        used for the call.
        """
        last_error = None

        for attempt in range(max_retries):
            key = await self.acquire(estimated_tokens)

            try:
                # Always inject the acquired key into the call
                kwargs["api_key"] = key.key

                result = await func(*args, **kwargs)
                key.record_success()
                return result

            except Exception as e:
                last_error = e
                # Only rate-limit/auth/server errors count against the key.
                key.record_error(e)
                logger.warning(f"Attempt {attempt + 1} failed with key {key.name}: {e}")

                # If rate limited, wait a bit before retry
                if classify_key_error(e) == "rate_limit":
                    await asyncio.sleep(2**attempt)  # Exponential backoff

        raise last_error

    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics for all keys."""
        return {
            "total_keys": len(self.keys),
            "healthy_keys": sum(1 for k in self.keys if k.is_healthy),
            "current_key": self.current_key_index,
            "keys": [
                {
                    "name": k.name,
                    "healthy": k.is_healthy,
                    "request_tokens": round(k.request_tokens, 2),
                    "token_tokens": round(k.token_tokens, 2),
                    "total_requests": k.total_requests,
                    "total_tokens": k.total_tokens,
                    "consecutive_errors": k.consecutive_errors,
                }
                for k in self.keys
            ],
        }

    @asynccontextmanager
    async def key_context(self, estimated_tokens: int = 1000):
        """Context manager for acquiring a key."""
        key = await self.acquire(estimated_tokens)
        try:
            yield key
        except Exception as e:
            key.record_error(e)
            raise
        else:
            key.record_success()


# Global instance
_rate_limit_manager: Optional[RateLimitManager] = None


async def get_rate_limit_manager() -> RateLimitManager:
    """The rate limit manager of the engine running in this context (#186),
    else the process-wide one (created on first call)."""
    engine = active_engine()
    if engine is not None:
        manager = engine.rate_limiter()
        await manager.initialize()
        return manager
    global _rate_limit_manager
    if _rate_limit_manager is None:
        _rate_limit_manager = RateLimitManager()
        await _rate_limit_manager.initialize()
    return _rate_limit_manager


async def close_rate_limit_manager():
    """Close the global rate limit manager."""
    global _rate_limit_manager
    if _rate_limit_manager:
        await _rate_limit_manager.shutdown()
        _rate_limit_manager = None


# Convenience function for backward compatibility
async def run_llm_with_rate_limit(
    system_prompt: str,
    user_prompt: str,
    model: str = "llama-3.3-70b-versatile",
    estimated_tokens: int = 2000,
) -> str:
    """
    Run LLM completion with automatic rate limit management.
    This replaces the old run_llm function in agents.py
    """
    from langchain_groq import ChatGroq
    from langchain_core.messages import SystemMessage, HumanMessage

    manager = await get_rate_limit_manager()

    async def _call_groq(api_key: str):
        llm = ChatGroq(model=model, api_key=api_key)
        response = await llm.ainvoke(
            [SystemMessage(content=system_prompt), HumanMessage(content=user_prompt)]
        )
        return response.content

    return await manager.execute_with_retry(
        _call_groq, estimated_tokens=estimated_tokens
    )


# Backward compatibility: get all keys (for existing code)
def get_all_groq_keys() -> List[str]:
    """Get all Groq API keys (legacy function)."""
    keys = []
    primary = os.getenv("GROQ_API_KEY")
    if primary:
        keys.append(primary)
    for i in range(1, 20):
        k = os.getenv(f"GROQ_API_KEY_{i}")
        if k:
            keys.append(k)
    return keys
