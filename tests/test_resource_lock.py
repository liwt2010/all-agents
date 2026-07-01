"""
Tests: Distributed ResourceLock
"""

import asyncio
import pytest
import time

from agent_system.concurrency.lock import (
    ResourceLock,
    LockRecord,
    LockBusyError,
    get_resource_lock,
)


class TestResourceLock:
    @pytest.mark.asyncio
    async def test_acquire_and_release(self):
        lock = ResourceLock()
        assert await lock.acquire("file:1", "alice") is True
        assert await lock.is_locked("file:1") is True
        assert await lock.get_holder("file:1") == "alice"
        assert await lock.release("file:1", "alice") is True
        assert await lock.is_locked("file:1") is False

    @pytest.mark.asyncio
    async def test_acquire_twice_same_holder(self):
        """Same holder can re-acquire (idempotent)."""
        lock = ResourceLock()
        assert await lock.acquire("file:1", "alice") is True
        assert await lock.acquire("file:1", "alice") is True

    @pytest.mark.asyncio
    async def test_different_holder_blocked(self):
        lock = ResourceLock()
        assert await lock.acquire("file:1", "alice") is True
        assert await lock.acquire("file:1", "bob") is False

    @pytest.mark.asyncio
    async def test_release_only_holder(self):
        lock = ResourceLock()
        await lock.acquire("file:1", "alice")
        # Bob can't release Alice's lock
        assert await lock.release("file:1", "bob") is False
        # Alice can
        assert await lock.release("file:1", "alice") is True

    @pytest.mark.asyncio
    async def test_ttl_expiry(self):
        lock = ResourceLock(default_ttl=1)
        assert await lock.acquire("file:1", "alice", ttl=1) is True
        # After 1.2s the lock is expired
        await asyncio.sleep(1.2)
        # Different holder can now grab it
        assert await lock.acquire("file:1", "bob") is True

    @pytest.mark.asyncio
    async def test_heartbeat_extends(self):
        lock = ResourceLock(default_ttl=1)
        await lock.acquire("file:1", "alice", ttl=1)
        # Heartbeat before expiry
        await asyncio.sleep(0.5)
        assert await lock.heartbeat("file:1", "alice", ttl=2) is True
        await asyncio.sleep(0.8)
        # Should still be locked
        assert await lock.is_locked("file:1") is True

    @pytest.mark.asyncio
    async def test_heartbeat_only_holder(self):
        lock = ResourceLock()
        await lock.acquire("file:1", "alice")
        assert await lock.heartbeat("file:1", "bob") is False

    @pytest.mark.asyncio
    async def test_with_lock_context_manager(self):
        lock = ResourceLock()
        async with lock.with_lock("file:1", "alice"):
            assert await lock.is_locked("file:1") is True
        assert await lock.is_locked("file:1") is False

    @pytest.mark.asyncio
    async def test_with_lock_raises_on_contention(self):
        lock = ResourceLock()
        await lock.acquire("file:1", "alice")
        with pytest.raises(LockBusyError) as exc_info:
            async with lock.with_lock("file:1", "bob"):
                pass
        assert exc_info.value.resource == "file:1"
        assert exc_info.value.current_holder == "alice"

    @pytest.mark.asyncio
    async def test_with_lock_releases_on_exception(self):
        lock = ResourceLock()
        try:
            async with lock.with_lock("file:1", "alice"):
                raise RuntimeError("test error")
        except RuntimeError:
            pass
        assert await lock.is_locked("file:1") is False

    @pytest.mark.asyncio
    async def test_concurrent_acquire_serializes(self):
        """Multiple concurrent acquires on same resource — only one wins."""
        lock = ResourceLock()
        results = await asyncio.gather(*[
            lock.acquire("file:1", f"agent-{i}") for i in range(10)
        ])
        winners = sum(1 for r in results if r)
        assert winners == 1

    @pytest.mark.asyncio
    async def test_stats(self):
        lock = ResourceLock()
        await lock.acquire("file:1", "alice", ttl=5)
        await lock.acquire("file:2", "bob", ttl=5)
        stats = await lock.stats()
        assert stats["total_locks"] == 2
        assert any(l["resource"] == "file:1" for l in stats["locks"])


class TestGlobalLock:
    def test_singleton(self):
        l1 = get_resource_lock()
        l2 = get_resource_lock()
        assert l1 is l2
