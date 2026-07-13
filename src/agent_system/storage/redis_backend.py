"""
Redis backend — distributed lock (PLATFORM §32.2) and quota store.

Uses the official `redis` Python client. Same acquire / release /
heartbeat / with_lock interface as the in-memory ResourceLock, but
the state lives in Redis so multiple processes / containers share it.

If REDIS_URL is not set, the in-memory ResourceLock is used as fallback.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

from agent_system.concurrency.lock import (
    ResourceLock,
    LockRecord,
    LockBusyError,
)

logger = logging.getLogger(__name__)


class RedisResourceLock:
    """
    Redis-backed distributed lock with TTL + heartbeat.

    Uses `SET key value NX EX ttl` for atomic acquire. Holder identity
    is stored as a string so release/heartbeat can verify ownership.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0",
                 default_ttl: int = 300,
                 fallback: ResourceLock | None = None):
        self.default_ttl = default_ttl
        self._fallback = fallback or ResourceLock(default_ttl)
        self._client = None

        try:
            import redis.asyncio as aioredis
            self._client = aioredis.from_url(redis_url, decode_responses=True)
        except Exception as e:
            logger.warning(f"Failed to initialize Redis client ({e}); fallback to in-memory")
            self._client = None

    async def _ensure_client(self):
        if self._client is None:
            return None
        try:
            await self._client.ping()
            return self._client
        except Exception:
            return None

    async def acquire(
        self,
        resource: str,
        holder: str,
        ttl: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        client = await self._ensure_client()
        if client is None:
            return await self._fallback.acquire(resource, holder, ttl=ttl, metadata=metadata)

        result = await client.set(
            f"lock:{resource}",
            holder,
            nx=True,
            ex=ttl or self.default_ttl,
        )
        if result:
            return True
        # Already locked — same holder can re-acquire (extend TTL)
        current = await client.get(f"lock:{resource}")
        if current == holder:
            await client.expire(f"lock:{resource}", ttl or self.default_ttl)
            return True
        return False

    async def release(self, resource: str, holder: str) -> bool:
        client = await self._ensure_client()
        if client is None:
            return await self._fallback.release(resource, holder)
        current = await client.get(f"lock:{resource}")
        if current != holder:
            return False
        await client.delete(f"lock:{resource}")
        return True

    async def heartbeat(
        self,
        resource: str,
        holder: str,
        ttl: int | None = None,
    ) -> bool:
        client = await self._ensure_client()
        if client is None:
            return await self._fallback.heartbeat(resource, holder, ttl=ttl)
        current = await client.get(f"lock:{resource}")
        if current != holder:
            return False
        return bool(await client.expire(f"lock:{resource}", ttl or self.default_ttl))

    async def is_locked(self, resource: str) -> bool:
        client = await self._ensure_client()
        if client is None:
            return await self._fallback.is_locked(resource)
        return bool(await client.exists(f"lock:{resource}"))

    async def get_holder(self, resource: str) -> str | None:
        client = await self._ensure_client()
        if client is None:
            return await self._fallback.get_holder(resource)
        return await client.get(f"lock:{resource}")

    @asynccontextmanager
    async def with_lock(
        self,
        resource: str,
        holder: str,
        ttl: int | None = None,
    ):
        acquired = await self.acquire(resource, holder, ttl=ttl)
        if not acquired:
            current = await self.get_holder(resource)
            raise LockBusyError(resource, current or "unknown")
        try:
            yield
        finally:
            await self.release(resource, holder)

    async def stats(self) -> dict[str, Any]:
        client = await self._ensure_client()
        if client is None:
            return await self._fallback.stats()
        cursor = 0
        locks = []
        while True:
            cursor, keys = await client.scan(cursor=cursor, match="lock:*", count=100)
            for key in keys:
                holder = await client.get(key)
                ttl = await client.ttl(key)
                locks.append({"resource": key[5:], "holder": holder, "ttl": ttl})
            if cursor == 0:
                break
        return {"total_locks": len(locks), "locks": locks}


# ── Redis Quota store (PLATFORM §28) ──

class RedisQuotaStore:
    """
    Quota store backed by Redis. Replaces the in-memory QuotaStore
    in `core/quota.py` for cross-process consistency.

    Falls back to in-memory if Redis is unavailable.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379/0"):
        try:
            import redis.asyncio as aioredis
            self._client = aioredis.from_url(redis_url, decode_responses=True)
        except Exception:
            self._client = None
        from agent_system.core.quota import QuotaStore as InMemQuotaStore
        self._fallback = InMemQuotaStore()

    async def _ensure(self):
        if self._client is None:
            return None
        try:
            await self._client.ping()
            return self._client
        except Exception:
            return None

    def record_usage_fallback(self, user_id: str, cost: float, tokens: int):
        """Helper: record via the in-memory fallback."""
        from agent_system.core.quota import UsageRecord
        self._fallback.record_usage(UsageRecord(
            user_id=user_id, cost=cost, tokens_input=tokens,
        ))

    async def get_daily_cost(self, user_id: str) -> float:
        client = await self._ensure()
        if client is None:
            return 0.0
        from datetime import date
        today = date.today().isoformat()
        return float(await client.get(f"quota:{user_id}:{today}:cost") or 0)

    async def get_daily_tokens(self, user_id: str) -> int:
        client = await self._ensure()
        if client is None:
            return 0
        from datetime import date
        today = date.today().isoformat()
        return int(await client.get(f"quota:{user_id}:{today}:tokens") or 0)

    async def increment(self, user_id: str, cost: float, tokens: int):
        """Atomic increment of today's usage in Redis."""
        client = await self._ensure()
        if client is None:
            self.record_usage_fallback(user_id, cost, tokens)
            return
        from datetime import date
        today = date.today().isoformat()
        cost_key = f"quota:{user_id}:{today}:cost"
        token_key = f"quota:{user_id}:{today}:tokens"
        async with client.pipeline() as pipe:
            pipe.incrbyfloat(cost_key, cost)
            pipe.incrby(token_key, tokens)
            pipe.expire(cost_key, 86400 * 2)
            pipe.expire(token_key, 86400 * 2)
            await pipe.execute()


# ── Factory ──

def create_redis_lock(
    redis_url: str | None = None,
    default_ttl: int = 300,
) -> RedisResourceLock:
    """Factory: returns RedisResourceLock with in-memory fallback."""
    fallback = ResourceLock(default_ttl=default_ttl)
    return RedisResourceLock(
        redis_url=redis_url or "redis://localhost:6379/0",
        default_ttl=default_ttl,
        fallback=fallback,
    )
