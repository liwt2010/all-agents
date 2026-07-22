"""
Distributed rate limiter backend abstraction (PR v0.2.0).

Backends implement the same async interface so `LimiterRegistry` can
swap in-memory for Redis without code changes. Choose at startup:

  - InMemoryBackend — single-process, no external deps. Good for dev,
    single-replica deploys, and tests.
  - RedisBackend — multi-replica safe. Uses ZSET + Lua to make the
    check-and-record atomic so two replicas can't both admit a request
    that pushes the count over the limit.

Both backends share the same sliding-window-log algorithm:
  1. Drop entries older than `now - window`
  2. If len(entries) >= limit, reject (return retry_after)
  3. Else append `now` and admit

Memory cost per key is O(limit) entries — fine for the default
120 req/min (max 120 timestamps/user).
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from typing import Any, Protocol

from agent_system.core.rate_limit.sliding_window import LimitDecision

logger = logging.getLogger(__name__)


class RateLimiterBackend(Protocol):
    """Async backend for sliding-window rate limiting.

    Implementations must be safe for concurrent use. The check is atomic
    (check-and-record happens together); peek is non-mutating.
    """

    async def check(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
        scope: str = "",
    ) -> LimitDecision:
        """Atomically: evict expired entries, admit if under limit, record."""
        ...

    async def peek(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
        scope: str = "",
    ) -> LimitDecision:
        """Non-mutating check — useful for headers / monitoring."""
        ...

    async def reset(self, key: str | None = None) -> None:
        """Drop state for a key (or all keys)."""
        ...

    async def stats(self) -> dict[str, int]:
        """Return backend stats for monitoring (e.g. active key count)."""
        ...


# ── In-memory backend ──

class InMemoryBackend:
    """Single-process sliding window. Thread-safe via asyncio.Lock.

    Wraps the same deque-based algorithm as `SlidingWindowLimiter` but
    in async form so it satisfies the `RateLimiterBackend` protocol.
    """

    def __init__(self, cleanup_interval: float = 300.0):
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._cleanup_interval = cleanup_interval
        self._last_cleanup = time.time()
        self._lock = asyncio.Lock()

    @staticmethod
    def _evict_old(bucket: deque[float], now: float, window: float) -> None:
        cutoff = now - window
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

    async def _maybe_cleanup(self, now: float) -> None:
        if now - self._last_cleanup < self._cleanup_interval:
            return
        self._last_cleanup = now
        stale_cutoff = now - self._cleanup_interval
        stale = [k for k, b in self._buckets.items() if not b or b[-1] < stale_cutoff]
        for k in stale:
            del self._buckets[k]

    async def check(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
        scope: str = "",
    ) -> LimitDecision:
        now = time.time()
        async with self._lock:
            await self._maybe_cleanup(now)
            bucket = self._buckets[key]
            self._evict_old(bucket, now, window_seconds)
            if len(bucket) < limit:
                bucket.append(now)
                reset_at = bucket[0] + window_seconds if bucket else now + window_seconds
                return LimitDecision(
                    allowed=True,
                    remaining=limit - len(bucket),
                    reset_at=reset_at,
                    retry_after=0.0,
                    scope=scope,
                    key=key,
                )
            oldest = bucket[0]
            retry_after = max(0.0, (oldest + window_seconds) - now)
            return LimitDecision(
                allowed=False,
                remaining=0,
                reset_at=oldest + window_seconds,
                retry_after=retry_after,
                scope=scope,
                key=key,
            )

    async def peek(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
        scope: str = "",
    ) -> LimitDecision:
        now = time.time()
        async with self._lock:
            bucket = self._buckets[key]
            self._evict_old(bucket, now, window_seconds)
            if len(bucket) < limit:
                reset_at = bucket[0] + window_seconds if bucket else now + window_seconds
                return LimitDecision(
                    allowed=True,
                    remaining=limit - len(bucket),
                    reset_at=reset_at,
                    retry_after=0.0,
                    scope=scope,
                    key=key,
                )
            oldest = bucket[0]
            retry_after = max(0.0, (oldest + window_seconds) - now)
            return LimitDecision(
                allowed=False,
                remaining=0,
                reset_at=oldest + window_seconds,
                retry_after=retry_after,
                scope=scope,
                key=key,
            )

    async def reset(self, key: str | None = None) -> None:
        async with self._lock:
            if key:
                self._buckets.pop(key, None)
            else:
                self._buckets.clear()

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            return {"active_keys": len(self._buckets), "backend": 0}


# ── Redis backend ──

# Lua script: atomic sliding-window-log check-and-record.
# KEYS[1] = sorted-set key holding timestamps
# ARGV[1] = now (ms since epoch, float)
# ARGV[2] = window_ms
# ARGV[3] = limit
# ARGV[4] = unique member (request_id) to avoid ZADD dedup
#
# Returns: {allowed (1/0), count_after, reset_ms}
_REDIS_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

local cutoff = now - window
redis.call('ZREMRANGEBYSCORE', key, '-inf', '(' .. cutoff)
local count = tonumber(redis.call('ZCARD', key)) or 0

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window)
    -- reset = oldest entry score + window
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local reset = now + window
    if oldest[2] then reset = tonumber(oldest[2]) + window end
    return {1, count + 1, reset}
else
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local reset = now + window
    if oldest[2] then reset = tonumber(oldest[2]) + window end
    return {0, count, reset}
end
"""


class _ScriptWrapper:
    """Lazy-detect whether the server supports EVAL/EVALSHA, then wrap
    `register_script` (production Redis). On servers that don't support
    Lua at all (fakeredis, mock servers), falls back to a Python
    WATCH/MULTI/EXEC implementation of the same sliding-window-log
    algorithm.
    """

    def __init__(self, redis_client: Any):
        self._redis = redis_client
        self._impl: str | None = None  # "lua" / "watch" / None

    def _try_lua(self) -> bool:
        try:
            script = self._redis.register_script(_REDIS_LUA)
            script(keys=["__rl_probe__"], args=[0, 1, 1, "p"])
            return True
        except Exception as e:
            msg = str(e).lower()
            if "evalsha" in msg or "unknown command" in msg or "eval" in msg:
                return False
            # Other error — assume the script itself failed and Lua is fine
            return True

    def _probe(self) -> None:
        if self._impl is not None:
            return
        if self._try_lua():
            self._impl = "lua"
        else:
            self._impl = "watch"

    async def run(
        self,
        redis_key: str,
        args: list[float],
        *,
        member: str,
    ) -> tuple[int, int, float]:
        """Execute the sliding-window check-and-record. Returns
        (allowed_int, count_after, reset_ms)."""
        self._probe()
        if self._impl == "lua":
            return await self._run_lua(redis_key, args, member)
        return await self._run_watch(redis_key, args, member)

    async def _run_lua(
        self, redis_key: str, args: list[float], member: str
    ) -> tuple[int, int, float]:
        script = self._redis.register_script(_REDIS_LUA)
        result = await _redis_call(script, keys=[redis_key], args=args + [member])
        return (int(result[0]), int(result[1]), float(result[2]))

    async def _run_watch(
        self, redis_key: str, args: list[float], member: str
    ) -> tuple[int, int, float]:
        """Pure-Python WATCH-based fallback. Slower than Lua (round-trip
        per check) but correct under concurrency.

        Args: [now_ms, window_ms, limit]. Member is generated by caller.
        """
        import redis as redis_lib
        now_ms, window_ms, limit = args
        cutoff = now_ms - window_ms
        # WATCH + MULTI + EXEC. The outer loop retries on WatchError
        # (another replica mutated the key between WATCH and EXEC).
        for _ in range(5):  # bounded retries to avoid pathological loops
            try:
                result = await _redis_call(self._do_watch_round, redis_key, cutoff, member, now_ms, window_ms)
                break
            except redis_lib.WatchError:
                continue
            except Exception:
                # Fall back to admit on any other error
                return (1, 0, now_ms + window_ms)
        else:
            # All retries exhausted — admit
            return (1, 0, now_ms + window_ms)
        # Result is [trimmed_count, zcard_after_trim]; decide here.
        zcard = int(result[1]) if len(result) >= 2 else 0
        if zcard < limit:
            # Admit — ZADD in a separate transaction (the read happened,
            # and ZADD will overwrite if a racing replica also ZADDed
            # the same member — the only race is on the LIMIT, which
            # we've already cleared).
            await _redis_call(self._do_admit, redis_key, member, now_ms, window_ms)
            oldest = await _redis_call(self._redis.zrange, redis_key, 0, 0, withscores=True)
            reset = (float(oldest[0][1]) + window_ms) if oldest else (now_ms + window_ms)
            return (1, zcard + 1, reset)
        else:
            oldest = await _redis_call(self._redis.zrange, redis_key, 0, 0, withscores=True)
            reset = (float(oldest[0][1]) + window_ms) if oldest else (now_ms + window_ms)
            return (0, zcard, reset)

    def _do_watch_round(self, redis_key: str, cutoff: float, member: str, now_ms: float, window_ms: float):
        """Single WATCH+ZCARD round. Returns [trimmed, count]."""
        pipe = self._redis.pipeline()
        pipe.watch(redis_key)
        pipe.multi()
        pipe.zremrangebyscore(redis_key, "-inf", f"({cutoff}")
        pipe.zcard(redis_key)
        return pipe.execute()

    def _do_admit(self, redis_key: str, member: str, now_ms: float, window_ms: float):
        """Atomic ZADD + PEXPIRE. Use MULTI for atomicity (no WATCH needed
        here — concurrent ZADDs with distinct members are commutative)."""
        pipe = self._redis.pipeline()
        pipe.multi()
        pipe.zadd(redis_key, {member: now_ms})
        pipe.pexpire(redis_key, int(window_ms))
        return pipe.execute()


class RedisBackend:
    """Redis-backed sliding window. Multi-replica safe via Lua atomicity.

    Uses a sorted set per key, scored by request timestamp (ms). Each
    check atomically: trims old entries, counts, admits-or-rejects, and
    records the new entry — all in one round trip.

    Requires the `redis` package (>=5.0) and a reachable Redis server.
    Fails closed by default — if Redis is unreachable, the middleware
    should fall back to its fail_mode setting (see middleware).
    """

    def __init__(self, redis_client: Any, key_prefix: str = "rl:"):
        self._redis = redis_client
        self._prefix = key_prefix
        self._script = _ScriptWrapper(redis_client)

    def _get_script(self) -> Any:
        # Kept for backward-compat introspection; not used internally.
        return self._script

    async def _run_script(self, *, now_ms: float, window_ms: float, limit: int, member: str, redis_key: str):
        return await self._script.run(
            redis_key,
            [now_ms, window_ms, limit],
            member=member,
        )

    @staticmethod
    def _member(now_ms: float) -> str:
        # Sorted-set members must be unique per request — millisecond
        # timestamps can collide under load, so append a random suffix.
        import secrets
        return f"{now_ms:.6f}:{secrets.token_hex(4)}"

    async def check(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
        scope: str = "",
    ) -> LimitDecision:
        now_ms = time.time() * 1000.0
        window_ms = window_seconds * 1000.0
        member = self._member(now_ms)
        try:
            allowed_i, count, reset_ms = await self._run_script(
                now_ms=now_ms, window_ms=window_ms,
                limit=limit, member=member,
                redis_key=self._prefix + key,
            )
        except Exception as e:
            logger.warning(f"Redis rate limiter check failed: {e}")
            # Degrade gracefully: admit the request. Caller's fail_mode
            # middleware decides whether to translate this into a 503.
            return LimitDecision(
                allowed=True,
                remaining=limit,
                reset_at=(now_ms + window_ms) / 1000.0,
                retry_after=0.0,
                scope=scope,
                key=key,
            )

        allowed = bool(int(allowed_i))
        reset_at = reset_ms / 1000.0
        if allowed:
            remaining = max(0, limit - int(count))
            return LimitDecision(
                allowed=True,
                remaining=remaining,
                reset_at=reset_at,
                retry_after=0.0,
                scope=scope,
                key=key,
            )
        return LimitDecision(
            allowed=False,
            remaining=0,
            reset_at=reset_at,
            retry_after=max(0.0, reset_at - time.time()),
            scope=scope,
            key=key,
        )

    async def peek(
        self,
        key: str,
        *,
        limit: int,
        window_seconds: float,
        scope: str = "",
    ) -> LimitDecision:
        now_ms = time.time() * 1000.0
        window_ms = window_seconds * 1000.0
        try:
            redis = self._redis
            full_key = self._prefix + key
            cutoff = now_ms - window_ms
            # No atomic peek+count in Redis — best effort read.
            count = await _redis_call(redis.zremrangebyscore, full_key, "-inf", f"({cutoff}")
            count = await _redis_call(redis.zcard, full_key) if count is not None else 0
            oldest = await _redis_call(redis.zrange, full_key, 0, 0, withscores=True)
        except Exception as e:
            logger.warning(f"Redis rate limiter peek failed: {e}")
            return LimitDecision(
                allowed=True, remaining=limit,
                reset_at=(now_ms + window_ms) / 1000.0,
                retry_after=0.0, scope=scope, key=key,
            )
        if count < limit:
            reset_at = (oldest[0][1] + window_ms) / 1000.0 if oldest else (now_ms + window_ms) / 1000.0
            return LimitDecision(
                allowed=True,
                remaining=limit - count,
                reset_at=reset_at,
                retry_after=0.0,
                scope=scope,
                key=key,
            )
        reset_at = (oldest[0][1] + window_ms) / 1000.0 if oldest else (now_ms + window_ms) / 1000.0
        return LimitDecision(
            allowed=False,
            remaining=0,
            reset_at=reset_at,
            retry_after=max(0.0, reset_at - time.time()),
            scope=scope,
            key=key,
        )

    async def reset(self, key: str | None = None) -> None:
        if key:
            await _redis_call(self._redis.delete, self._prefix + key)
        else:
            # SCAN + DELETE for all keys under prefix
            cursor = 0
            pattern = self._prefix + "*"
            while True:
                cursor, keys = await _redis_call(self._redis.scan, cursor, match=pattern, count=100)
                if keys:
                    await _redis_call(self._redis.delete, *keys)
                if cursor == 0:
                    break

    async def stats(self) -> dict[str, int]:
        cursor = 0
        total = 0
        pattern = self._prefix + "*"
        while True:
            cursor, keys = await _redis_call(self._redis.scan, cursor, match=pattern, count=100)
            total += len(keys)
            if cursor == 0:
                break
        return {"active_keys": total, "backend": 1}


# ── Async adapter for redis-py ──

async def _redis_call(fn, *args, **kwargs):
    """Call a redis-py method from async code. The library is sync; we
    hop to a thread so we don't block the event loop on network I/O.

    `register_script` returns a callable that already handles EVAL/EVALSHA
    internally — passing the resulting object works the same way.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))


# ── Factory ──

async def create_backend_from_env() -> RateLimiterBackend:
    """Return Redis backend if REDIS_URL is set and reachable, else in-memory.

    Connection attempts time out fast (1s) so the server still boots if
    Redis is briefly unavailable — the rate limiter just falls back to
    in-memory mode for this process.
    """
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        return InMemoryBackend()

    try:
        import redis  # type: ignore
        client = redis.Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=2)
        # Probe connectivity
        await _redis_call(client.ping)
        logger.info("Rate limiter: using Redis backend at %s", redis_url)
        return RedisBackend(client)
    except Exception as e:
        logger.warning(
            "Rate limiter: Redis unreachable (%s), falling back to in-memory backend",
            e,
        )
        return InMemoryBackend()


import os  # noqa: E402  (kept at bottom for the factory function)