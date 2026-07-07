"""
Storage backend factory + configuration.

Usage:
    from agent_system.memory.storage import get_storage

    storage = get_storage()  # reads from settings (AGENT_STORAGE_BACKEND env var)
    storage.init()
    storage.save_node(node)
"""

import logging
import os
from typing import Optional

from agent_system.memory.storage.base import GraphStorage

logger = logging.getLogger(__name__)


def get_storage(
    backend: Optional[str] = None,
    **kwargs,
) -> GraphStorage:
    """
    Factory for storage backends.

    Args:
        backend: "json" | "sqlite" | "postgres". Falls back to AGENT_STORAGE_BACKEND env var,
                 then to "json" for development safety.
        **kwargs: Backend-specific args (db_path, host, port, ...)

    Returns:
        Initialized GraphStorage instance (call .init() to ensure schema).
    """
    backend = (
        backend
        or os.environ.get("AGENT_STORAGE_BACKEND")
        or "json"
    ).lower()

    if backend == "json":
        from agent_system.memory.storage.json_backend import JSONBackend
        return JSONBackend(
            base_dir=kwargs.get("base_dir") or os.environ.get("AGENT_JSON_DIR", "./data/graph"),
        )
    elif backend == "sqlite":
        from agent_system.memory.storage.sqlite_backend import SQLiteBackend
        return SQLiteBackend(
            db_path=kwargs.get("db_path") or os.environ.get("AGENT_SQLITE_PATH", "./data/graph.db"),
        )
    elif backend == "postgres":
        from agent_system.memory.storage.postgres_backend import PostgresBackend
        return PostgresBackend(
            host=kwargs.get("host") or os.environ.get("AGENT_POSTGRES_HOST", "localhost"),
            port=int(kwargs.get("port") or os.environ.get("AGENT_POSTGRES_PORT", 5432)),
            database=kwargs.get("database") or os.environ.get("AGENT_POSTGRES_DB", "all_agents"),
            user=kwargs.get("user") or os.environ.get("AGENT_POSTGRES_USER", "all_agents"),
            password=kwargs.get("password") or os.environ.get("AGENT_POSTGRES_PASSWORD"),
            pool_min=int(kwargs.get("pool_min") or os.environ.get("AGENT_POSTGRES_POOL_MIN", 2)),
            pool_max=int(kwargs.get("pool_max") or os.environ.get("AGENT_POSTGRES_POOL_MAX", 20)),
        )
    else:
        raise ValueError(
            f"Unknown storage backend: {backend!r}. "
            f"Valid options: json, sqlite, postgres"
        )