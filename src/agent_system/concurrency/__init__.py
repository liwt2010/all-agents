"""Concurrency subpackage — distributed lock (PLATFORM §32.2)."""
from agent_system.concurrency.lock import (
    ResourceLock,
    LockRecord,
    LockBusyError,
    get_resource_lock,
)

__all__ = ["ResourceLock", "LockRecord", "LockBusyError", "get_resource_lock"]
