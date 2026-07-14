"""Shared state for API routes.

Holds singleton instances that are accessed by multiple route modules.
This file is imported by both server.py (which initializes app-level singletons)
and the individual route modules (which consume them).

Why a separate state file?
    - Avoids circular imports (routes -> server -> routes)
    - Single source of truth for singleton instances
    - Easy to mock in tests via dependency_overrides

Lifecycle:
    - State is initialized at module import time (lazily for testability)
    - Same singletons are shared across all routes within a process
    - Per-request state stays in FastAPI dependency injection
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Set

from fastapi import WebSocket

from agent_system.core.audit_logger import get_audit_logger
from agent_system.core.auth import get_auth_service
from agent_system.core.checkpoint_tracker import CheckpointTracker, LiveProgress
from agent_system.core.security import InputSanitizer
from agent_system.storage.task_store import get_task_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle timestamps
# ---------------------------------------------------------------------------
_start_time: datetime = datetime.now(timezone.utc)


def get_start_time() -> datetime:
    return _start_time


# ---------------------------------------------------------------------------
# Singleton service instances
# ---------------------------------------------------------------------------
_task_store = get_task_store()
_checkpoint_tracker = CheckpointTracker()
_auth_service = get_auth_service()
_audit_logger = get_audit_logger()
_sanitizer = InputSanitizer()  # INSTANCE, not class (see PR-16)


def get_task_store_singleton():
    return _task_store


def get_checkpoint_tracker_singleton():
    return _checkpoint_tracker


def get_auth_service_singleton():
    return _auth_service


def get_audit_logger_singleton():
    return _audit_logger


def get_sanitizer_singleton():
    return _sanitizer


# ---------------------------------------------------------------------------
# In-flight task registry (for graceful shutdown)
# ---------------------------------------------------------------------------
_in_flight_tasks: Set[asyncio.Task] = set()


def get_in_flight_tasks() -> Set[asyncio.Task]:
    return _in_flight_tasks


# ---------------------------------------------------------------------------
# WebSocket connection registry
# ---------------------------------------------------------------------------
_ws_connections: Dict[str, List[WebSocket]] = {}


def get_ws_connections() -> Dict[str, List[WebSocket]]:
    return _ws_connections


# ---------------------------------------------------------------------------
# AutoGen (PEER path upgrade) capability cache
# Imported lazily to avoid hard dep on autogen
# ---------------------------------------------------------------------------
def get_has_autogen() -> bool:
    """Cached probe of whether AutoGen 0.4+ is installed."""
    from agent_system.core.autogen_discussion import HAS_AUTOGEN
    return HAS_AUTOGEN
