"""
Async batching + connection pooling utilities.

  - AsyncTaskPool: bounded-concurrency executor for task submissions
  - LLMClientPool: HTTP connection pool for the Anthropic SDK

Production goals:
  - Submit multiple tasks concurrently with a configurable worker count
  - Limit concurrent LLM API calls (respect the rate limits)
  - Avoid blocking the event loop with sync operations
"""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, List, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class AsyncTaskPool:
    """
    Bounded-concurrency task pool. Use for bulk operations
    (e.g. submit N tasks in parallel with a max-worker cap).
    """

    def __init__(self, max_workers: int = 10, name: str = "pool"):
        self.max_workers = max_workers
        self.name = name
        self._semaphore = asyncio.Semaphore(max_workers)
        self._active = 0
        self._completed = 0
        self._failed = 0
        self._lock = asyncio.Lock()

    async def submit(self, coro: Awaitable[T]) -> T:
        """Submit a coroutine to the pool. Blocks if at capacity."""
        async with self._semaphore:
            self._active += 1
            try:
                result = await coro
                self._completed += 1
                return result
            except Exception:
                self._failed += 1
                raise
            finally:
                self._active -= 1

    async def map(
        self,
        fn: Callable[..., Awaitable[T]],
        items: List[Any],
    ) -> List[T]:
        """Apply fn to each item, with bounded concurrency."""
        coros = [fn(item) for item in items]
        return await asyncio.gather(
            *(self.submit(c) for c in coros),
            return_exceptions=True,
        )

    def stats(self) -> dict:
        return {
            "name": self.name,
            "max_workers": self.max_workers,
            "active": self._active,
            "completed": self._completed,
            "failed": self._failed,
        }


# ── LLM call rate limiter ──

class LLMRateLimiter:
    """
    Token-bucket rate limiter for the Anthropic API.
    Default: 60 requests/minute. Adjustable per environment.
    """

    def __init__(self, rate_per_minute: int = 60):
        self.rate_per_minute = rate_per_minute
        self.interval_seconds = 60.0 / rate_per_minute
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def acquire(self) -> None:
        """Block until we can make another API call."""
        async with self._lock:
            now = time.monotonic()
            wait = (self._last_call + self.interval_seconds) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.monotonic()


# ── Global pool instances ──

_default_task_pool: Optional[AsyncTaskPool] = None
_default_llm_limiter: Optional[LLMRateLimiter] = None


def get_task_pool(max_workers: int = 10) -> AsyncTaskPool:
    global _default_task_pool
    if _default_task_pool is None:
        workers = int(os.environ.get("MAX_TASK_WORKERS", str(max_workers)))
        _default_task_pool = AsyncTaskPool(max_workers=workers, name="default")
    return _default_task_pool


def get_llm_rate_limiter() -> LLMRateLimiter:
    global _default_llm_limiter
    if _default_llm_limiter is None:
        rate = int(os.environ.get("LLM_RATE_PER_MINUTE", "60"))
        _default_llm_limiter = LLMRateLimiter(rate_per_minute=rate)
    return _default_llm_limiter


def reset_pools():
    global _default_task_pool, _default_llm_limiter
    _default_task_pool = None
    _default_llm_limiter = None
