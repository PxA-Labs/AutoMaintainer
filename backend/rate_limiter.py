"""
Rate Limit Manager for AutoMaintainer
Provides token bucket rate limiting with multi-key rotation for Groq API.
"""
import os
import time
import asyncio
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from collections import deque
from contextlib import asynccontextmanager
import threading

logger = logging.getLogger(__name__)


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
    
    # Health tracking
    consecutive_errors: int = 0
    last_error_time: float = 0
    is_healthy: bool = True
    total_requests: int = 0
    total_tokens: int = 0
    
    def refill(self, now: float = None):
        """Refill token buckets based on elapsed time."""
        now = now or time.time()
        elapsed = now - self.last_refill
        if elapsed > 0:
            # Refill requests (per minute)
            self.request_tokens = min(
                self.requests_per_minute,
                self.request_tokens + (self.requests_per_minute * elapsed / 60.0)
            )
            # Refill tokens (per minute)
            self.token_tokens = min(
                self.tokens_per_minute,
                self.token_tokens + (self.tokens_per_minute * elapsed / 60.0)
            )
            self.last_refill = now
    
    def can_make_request(self, estimated_tokens: int = 1000) -> bool:
        """Check if we have capacity for a request."""
        self.refill()
        return (self.request_tokens >= 1 and 
                self.token_tokens >= estimated_tokens and
                self.is_healthy)
    
    def consume(self, tokens_used: int = 0):
        """Consume tokens for a request."""
        self.refill()
        self.request_tokens -= 1
        if tokens_used > 0:
            self.token_tokens -= tokens_used
        self.total_requests += 1
        self.total_tokens += max(tokens_used, 0)
    
    def record_error(self, error: Exception):
        """Record an error and potentially mark unhealthy."""
        self.consecutive_errors += 1
        self.last_error_time = time.time()
        
        # Check for rate limit errors
        error_str = str(error).lower()
        if "rate limit" in error_str or "429" in error_str:
            # Drain request bucket on rate limit
            self.request_tokens = 0
            logger.warning(f"Rate limit hit for key {self.name}, draining request bucket")
        
        # Mark unhealthy after 3 consecutive errors
        if self.consecutive_errors >= 3:
            self.is_healthy = False
            logger.error(f"Key {self.name} marked unhealthy after {self.consecutive_errors} errors")
    
    def record_success(self):
        """Record successful request."""
        self.consecutive_errors = 0
        if not self.is_healthy:
            self.is_healthy = True
            logger.info(f"Key {self.name} recovered, marked healthy")
    
    def time_until_ready(self, estimated_tokens: int = 1000) -> float:
        """Estimate seconds until key can handle a request."""
        self.refill()
        if self.can_make_request(estimated_tokens):
            return 0.0
        
        # Time to get 1 request token
        req_wait = 0
        if self.request_tokens < 1:
            req_wait = (1 - self.request_tokens) * 60.0 / self.requests_per_minute
        
        # Time to get enough token tokens
        token_wait = 0
        if self.token_tokens < estimated_tokens:
            token_wait = (estimated_tokens - self.token_tokens) * 60.0 / self.tokens_per_minute
        
        return max(req_wait, token_wait)


class RateLimitManager:
    """
    Manages multiple Groq API keys with token bucket rate limiting
    and automatic key rotation.
    """
    
    def __init__(self):
        self.keys: List[APIKeyState] = []
        self.current_key_index: int = 0
        self._lock = asyncio.Lock()
        self._initialized = False
        self._health_check_task: Optional[asyncio.Task] = None
        
        # Configuration
        self.min_healthy_keys = 1
        self.recovery_check_interval = 30  # seconds
        self.default_request_rpm = int(os.getenv("GROQ_REQUESTS_PER_MINUTE", "30"))
        self.default_token_rpm = int(os.getenv("GROQ_TOKENS_PER_MINUTE", "6000"))
    
    async def initialize(self):
        """Load API keys from environment."""
        if self._initialized:
            return
        
        keys = []
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
        
        # Start health check background task
        self._health_check_task = asyncio.create_task(self._health_check_loop())
    
    async def _health_check_loop(self):
        """Periodically check and recover unhealthy keys."""
        while True:
            await asyncio.sleep(self.recovery_check_interval)
            async with self._lock:
                for key_state in self.keys:
                    if not key_state.is_healthy:
                        # Check if enough time has passed since last error
                        if time.time() - key_state.last_error_time > 120:  # 2 minutes
                            key_state.is_healthy = True
                            key_state.consecutive_errors = 0
                            logger.info(f"Key {key_state.name} auto-recovered after cooldown")
    
    async def shutdown(self):
        """Clean up background tasks."""
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
    
    def _get_next_healthy_key(self, estimated_tokens: int = 1000) -> Optional[APIKeyState]:
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
        self, 
        estimated_tokens: int = 1000,
        timeout: float = 60.0
    ) -> APIKeyState:
        """
        Acquire an API key for making a request.
        Waits if all keys are rate limited.
        """
        if not self._initialized:
            await self.initialize()
        
        start_time = time.time()
        
        while True:
            async with self._lock:
                key = self._get_next_healthy_key(estimated_tokens)
                if key:
                    key.consume(estimated_tokens)
                    return key
            
            # All keys busy - wait for the fastest one
            async with self._lock:
                wait_times = [
                    (k.time_until_ready(estimated_tokens), k) 
                    for k in self.keys if k.is_healthy
                ]
            
            if not wait_times:
                # No healthy keys at all
                raise RuntimeError("No healthy API keys available")
            
            min_wait, _ = min(wait_times, key=lambda x: x[0])
            wait_time = min(min_wait, 5.0)  # Cap at 5 seconds per check
            
            if time.time() - start_time + wait_time > timeout:
                raise TimeoutError(f"Rate limit timeout after {timeout}s")
            
            await asyncio.sleep(wait_time)
    
    async def execute_with_retry(
        self,
        func,
        *args,
        estimated_tokens: int = 1000,
        max_retries: int = 3,
        **kwargs
    ) -> Any:
        """
        Execute a function with automatic rate limit handling and key rotation.
        """
        last_error = None
        
        for attempt in range(max_retries):
            key = await self.acquire(estimated_tokens)
            
            try:
                # Inject the API key into the call
                if "api_key" in kwargs:
                    kwargs["api_key"] = key.key
                else:
                    # Try to pass as first arg if it's a client constructor
                    pass
                
                result = await func(*args, **kwargs)
                key.record_success()
                return result
                
            except Exception as e:
                last_error = e
                key.record_error(e)
                logger.warning(f"Attempt {attempt + 1} failed with key {key.name}: {e}")
                
                # If rate limited, wait a bit before retry
                if "429" in str(e) or "rate limit" in str(e).lower():
                    await asyncio.sleep(2 ** attempt)  # Exponential backoff
        
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
            ]
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
    """Get or create the global rate limit manager."""
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
    estimated_tokens: int = 2000
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
        response = await llm.ainvoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt)
        ])
        return response.content
    
    return await manager.execute_with_retry(
        _call_groq,
        estimated_tokens=estimated_tokens
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