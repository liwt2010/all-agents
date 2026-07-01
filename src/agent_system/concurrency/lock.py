"""
Distributed ResourceLock — PLATFORM §32.2

In-process equivalent of Redis SET NX EX. Production would use Redis
or a real distributed lock service; here we use an in-memory store
that has the same semantics (atomic check-and-set, expiry, heartbeat).

Use cases:
  - Two agents trying to edit the same file
  - Two users writing the same task
  - Migration + observer conflict avoidance
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class LockRecord(BaseModel):
    """An acquired lock."""
    resource: str
    holder: str
    acquired_at: float
    expires_at: float
    metadata: Dict[str, Any] = Field(default_factory=dict)


class LockBusyError(Exception):
    """Raised when a lock cannot be acquired."""
    def __init__(self, resource: str, current_holder: str):
        super().__init__(f"Resource {resource!r} is locked by {current_holder!r}")
        self.resource = resource
        self.current_holder = current_holder


class ResourceLock:
    """
    In-memory distributed lock with TTL + heartbeat.

    For production swap the underlying store with a Redis client.
    The interface (acquire / release / heartbeat / with_lock) is
    the same.
    """

    def __init__(self, default_ttl: int = 300):
        self._locks: Dict[str, LockRecord] = {}
        self.default_ttl = default_ttl
        self._lock = asyncio.Lock()  # for atomicity within this process

    async def _gc(self):
        """Garbage-collect expired locks."""
        now = time.time()
        expired = [r for r, rec in self._locks.items() if rec.expires_at <= now]
        for r in expired:
            del self._locks[r]

    async def acquire(
        self,
        resource: str,
        holder: str,
        ttl: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Try to acquire a lock. Returns True on success, False on contention."""
        async with self._lock:
            await self._gc()
            if resource in self._locks:
                existing = self._locks[resource]
                if existing.holder != holder and existing.expires_at > time.time():
                    return False
            now = time.time()
            self._locks[resource] = LockRecord(
                resource=resource,
                holder=holder,
                acquired_at=now,
                expires_at=now + (ttl or self.default_ttl),
                metadata=metadata or {},
            )
            return True

    async def release(self, resource: str, holder: str) -> bool:
        """Release a lock. Only the holder can release it."""
        async with self._lock:
            existing = self._locks.get(resource)
            if not existing:
                return False
            if existing.holder != holder:
                return False
            del self._locks[resource]
            return True

    async def heartbeat(
        self,
        resource: str,
        holder: str,
        ttl: Optional[int] = None,
    ) -> bool:
        """Extend the lock TTL. Must be the holder."""
        async with self._lock:
            existing = self._locks.get(resource)
            if not existing or existing.holder != holder:
                return False
            existing.expires_at = time.time() + (ttl or self.default_ttl)
            return True

    async def is_locked(self, resource: str) -> bool:
        """Check if a resource is currently locked."""
        async with self._lock:
            await self._gc()
            return resource in self._locks

    async def get_holder(self, resource: str) -> Optional[str]:
        async with self._lock:
            await self._gc()
            rec = self._locks.get(resource)
            return rec.holder if rec else None

    @asynccontextmanager
    async def with_lock(
        self,
        resource: str,
        holder: str,
        ttl: Optional[int] = None,
    ):
        """Context manager: acquire lock, run body, release (or raise on contention)."""
        acquired = await self.acquire(resource, holder, ttl=ttl)
        if not acquired:
            current = await self.get_holder(resource)
            raise LockBusyError(resource, current or "unknown")
        try:
            yield
        finally:
            await self.release(resource, holder)

    async def stats(self) -> Dict[str, Any]:
        async with self._lock:
            await self._gc()
            return {
                "total_locks": len(self._locks),
                "locks": [
                    {"resource": r, "holder": rec.holder, "ttl_remaining": rec.expires_at - time.time()}
                    for r, rec in self._locks.items()
                ],
            }


# Global instance
_default_lock: Optional[ResourceLock] = None


def get_resource_lock() -> ResourceLock:
    global _default_lock
    if _default_lock is None:
        _default_lock = ResourceLock()
    return _default_lock
