"""
Sliding window log rate limiter (PR-12).

Stores timestamps of recent requests per key. More accurate than token bucket
for short bursts because there's no token accumulation.

Trade-off: O(limit) memory per key vs O(1) for token bucket.
For default 120 req/min this is fine (max 120 timestamps per user).

Thread-safe via single mutex (sufficient for moderate concurrency).
"""

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional


@dataclass
class LimitDecision:
    """Result of a rate limit check."""
    allowed: bool
    remaining: int
    reset_at: float         # epoch seconds when oldest entry will expire
    retry_after: float      # seconds until next slot frees (0 if allowed)
    scope: str = ""
    key: str = ""


class SlidingWindowLimiter:
    """
    Sliding window log rate limiter.

    For each key, keeps a deque of request timestamps within the last `window`
    seconds. A request is allowed iff len(deque) < limit.
    """

    def __init__(
        self,
        limit: int = 60,
        window_seconds: float = 60.0,
        scope: str = "default",
        cleanup_interval: float = 300.0,
    ):
        if limit <= 0:
            raise ValueError(f"limit must be > 0, got {limit}")
        if window_seconds <= 0:
            raise ValueError(f"window_seconds must be > 0, got {window_seconds}")
        self.limit = limit
        self.window = window_seconds
        self.scope = scope
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.time()

    def _evict_old(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self.window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    def _maybe_cleanup(self, now: float) -> None:
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        # Drop keys whose newest entry is older than 2*window
        stale_cutoff = now - self.window * 2
        stale = [k for k, b in self._buckets.items() if not b or b[-1] < stale_cutoff]
        for k in stale:
            del self._buckets[k]

    def check(self, key: str, now: float | None = None) -> LimitDecision:
        """
        Check whether a request for `key` is allowed.

        Side effect: if allowed, records the timestamp.
        """
        now = now if now is not None else time.time()
        with self._lock:
            self._maybe_cleanup(now)
            bucket = self._buckets[key]
            self._evict_old(bucket, now)
            if len(bucket) < self.limit:
                bucket.append(now)
                # Reset = when oldest entry expires (next slot opens)
                reset_at = bucket[0] + self.window if bucket else now + self.window
                return LimitDecision(
                    allowed=True,
                    remaining=self.limit - len(bucket),
                    reset_at=reset_at,
                    retry_after=0.0,
                    scope=self.scope,
                    key=key,
                )
            else:
                oldest = bucket[0]
                retry_after = max(0.0, (oldest + self.window) - now)
                return LimitDecision(
                    allowed=False,
                    remaining=0,
                    reset_at=oldest + self.window,
                    retry_after=retry_after,
                    scope=self.scope,
                    key=key,
                )

    def peek(self, key: str, now: float | None = None) -> LimitDecision:
        """Check without recording. Useful for inspection / tests."""
        now = now if now is not None else time.time()
        with self._lock:
            bucket = self._buckets[key]
            self._evict_old(bucket, now)
            if len(bucket) < self.limit:
                reset_at = bucket[0] + self.window if bucket else now + self.window
                return LimitDecision(
                    allowed=True,
                    remaining=self.limit - len(bucket),
                    reset_at=reset_at,
                    retry_after=0.0,
                    scope=self.scope,
                    key=key,
                )
            else:
                oldest = bucket[0]
                retry_after = max(0.0, (oldest + self.window) - now)
                return LimitDecision(
                    allowed=False,
                    remaining=0,
                    reset_at=oldest + self.window,
                    retry_after=retry_after,
                    scope=self.scope,
                    key=key,
                )

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key:
                self._buckets.pop(key, None)
            else:
                self._buckets.clear()

    def stats(self) -> dict[str, int]:
        """Return count of active keys (for monitoring)."""
        with self._lock:
            return {"active_keys": len(self._buckets)}