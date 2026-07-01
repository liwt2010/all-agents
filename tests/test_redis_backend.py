"""
Tests: Redis backend (with in-memory fallback when no Redis)
"""

import pytest
import asyncio

from agent_system.storage.redis_backend import (
    RedisResourceLock,
    RedisQuotaStore,
    create_redis_lock,
)
from agent_system.concurrency.lock import LockBusyError, ResourceLock


class TestRedisResourceLock:
    """Test against a non-existent Redis — should fall back to in-memory."""

    @pytest.mark.asyncio
    async def test_acquire_via_fallback(self):
        lock = RedisResourceLock("redis://nonexistent:6379/0", default_ttl=60)
        assert await lock.acquire("r1", "alice") is True
        assert await lock.is_locked("r1") is True

    @pytest.mark.asyncio
    async def test_release_via_fallback(self):
        lock = RedisResourceLock("redis://nonexistent:6379/0")
        await lock.acquire("r1", "alice")
        assert await lock.release("r1", "alice") is True
        assert await lock.is_locked("r1") is False

    @pytest.mark.asyncio
    async def test_with_lock_via_fallback(self):
        lock = RedisResourceLock("redis://nonexistent:6379/0")
        async with lock.with_lock("r1", "alice"):
            assert await lock.is_locked("r1") is True
        assert await lock.is_locked("r1") is False

    @pytest.mark.asyncio
    async def test_heartbeat_via_fallback(self):
        lock = RedisResourceLock("redis://nonexistent:6379/0", default_ttl=1)
        await lock.acquire("r1", "alice", ttl=1)
        await asyncio.sleep(0.5)
        assert await lock.heartbeat("r1", "alice", ttl=5) is True

    @pytest.mark.asyncio
    async def test_contention_via_fallback(self):
        lock = RedisResourceLock("redis://nonexistent:6379/0")
        await lock.acquire("r1", "alice")
        with pytest.raises(LockBusyError):
            async with lock.with_lock("r1", "bob"):
                pass

    @pytest.mark.asyncio
    async def test_stats(self):
        lock = RedisResourceLock("redis://nonexistent:6379/0")
        await lock.acquire("r1", "alice")
        stats = await lock.stats()
        assert stats["total_locks"] >= 1

    @pytest.mark.asyncio
    async def test_get_holder_via_fallback(self):
        lock = RedisResourceLock("redis://nonexistent:6379/0")
        await lock.acquire("r1", "alice")
        assert await lock.get_holder("r1") == "alice"
        assert await lock.get_holder("nonexistent") is None


class TestRedisQuotaStore:
    """When Redis is unavailable, the store should not crash and report 0 usage."""

    @pytest.mark.asyncio
    async def test_get_daily_no_data_via_fallback(self):
        store = RedisQuotaStore("redis://nonexistent:6379/0")
        cost = await store.get_daily_cost("nobody")
        tokens = await store.get_daily_tokens("nobody")
        # Fallback is a no-op — return 0s
        assert cost == 0.0
        assert tokens == 0

    @pytest.mark.asyncio
    async def test_increment_doesnt_crash_via_fallback(self):
        store = RedisQuotaStore("redis://nonexistent:6379/0")
        # No exception expected
        await store.increment("u1", 0.5, 1000)
        # Increment was a no-op in fallback
        cost = await store.get_daily_cost("u1")
        assert cost == 0.0  # no real persistence

    @pytest.mark.asyncio
    async def test_multiple_increments_no_crash(self):
        store = RedisQuotaStore("redis://nonexistent:6379/0")
        for _ in range(3):
            await store.increment("u1", 0.1, 100)
        # Still no-op
        assert await store.get_daily_cost("u1") == 0.0


class TestFactory:
    def test_create_redis_lock_returns_instance(self):
        lock = create_redis_lock(redis_url="redis://nonexistent:6379/0")
        assert isinstance(lock, RedisResourceLock)
        assert lock._fallback is not None
        assert isinstance(lock._fallback, ResourceLock)
