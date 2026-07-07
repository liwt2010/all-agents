"""Storage backend abstraction for MultiLinkGraph.

Provides Protocol + 3 implementations:
- JSON: import/export + dev fallback (file-per-node)
- SQLite: single-file embedded DB (dev / small prod)
- PostgreSQL: production multi-instance

See docs/STORAGE.md for full design.
"""

from agent_system.memory.storage.base import GraphStorage
from agent_system.memory.storage.factory import get_storage

__all__ = ["GraphStorage", "get_storage"]