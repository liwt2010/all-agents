"""
SQLite storage backend — single-file embedded database.

Best for:
  - Development (zero-ops, just a file)
  - Single-node production (small scale)
  - Testing (no server dependency)

Not for:
  - Multi-instance production (no concurrent writes)
  - Large graphs > 100k nodes (consider PostgreSQL)
"""

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from agent_system.memory.graph import (
    GraphLink,
    GraphNode,
    LinkType,
    NodeType,
)
from agent_system.observability.instrumentation import track_storage

if TYPE_CHECKING:
    from agent_system.memory.graph import MultiLinkGraph

logger = logging.getLogger(__name__)


# SQLite type adapter: store datetime as ISO string
def _adapt_datetime(dt):
    return dt.isoformat()


def _convert_datetime(val):
    from datetime import datetime
    return datetime.fromisoformat(val)


sqlite3.register_adapter(__import__("datetime").datetime, _adapt_datetime)
sqlite3.register_converter("timestamp", _convert_datetime)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_nodes (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    content     TEXT NOT NULL,            -- JSON string
    metadata    TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON graph_nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_created_at ON graph_nodes(created_at);

CREATE TABLE IF NOT EXISTS graph_links (
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    link_type   TEXT NOT NULL,
    weight      REAL DEFAULT 1.0,
    context     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    created_by  TEXT DEFAULT 'system',
    PRIMARY KEY (source_id, target_id, link_type)
);
CREATE INDEX IF NOT EXISTS idx_links_source ON graph_links(source_id);
CREATE INDEX IF NOT EXISTS idx_links_target ON graph_links(target_id);
CREATE INDEX IF NOT EXISTS idx_links_type ON graph_links(link_type);
"""


class SQLiteBackend:
    """SQLite-backed graph storage. Thread-safe via per-thread connections."""

    def __init__(self, db_path: str = "./data/graph.db"):
        self.db_path = str(db_path)
        # Per-thread connection (SQLite is thread-safe with serialized writes)
        self._local = threading.local()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(
                self.db_path,
                detect_types=sqlite3.PARSE_DECLTYPES,
                check_same_thread=False,
                timeout=30.0,
            )
            # WAL mode for concurrent reads + serialized writes
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def backend_name(self) -> str:
        return f"sqlite:{Path(self.db_path).name}"

    def ping(self) -> bool:
        try:
            self._conn().execute("SELECT 1").fetchone()
            return True
        except Exception as e:
            logger.warning(f"SQLiteBackend ping failed: {e}")
            return False

    def init(self) -> None:
        """Create tables and indexes (idempotent)."""
        with self._conn() as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn is not None:
            self._local.conn.close()
            self._local.conn = None

    def _row_to_node(self, row) -> GraphNode:
        return GraphNode(
            id=row["id"],
            type=NodeType(row["type"]),
            content=json.loads(row["content"]),
            metadata=json.loads(row["metadata"]),
            created_at=__import__("datetime").datetime.fromisoformat(row["created_at"]),
            updated_at=__import__("datetime").datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_link(self, row) -> GraphLink:
        return GraphLink(
            source_id=row["source_id"],
            target_id=row["target_id"],
            link_type=LinkType(row["link_type"]),
            weight=row["weight"],
            context=json.loads(row["context"]),
            created_at=__import__("datetime").datetime.fromisoformat(row["created_at"]),
            created_by=row["created_by"],
        )

    # ── Node operations ──

    @track_storage(backend="sqlite", op="save_node")
    def save_node(self, node: GraphNode) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO graph_nodes (id, type, content, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    type = excluded.type,
                    content = excluded.content,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    node.id,
                    node.type.value,
                    json.dumps(node.content, default=str),
                    json.dumps(node.metadata, default=str),
                    node.created_at.isoformat(),
                    node.updated_at.isoformat(),
                ),
            )
            conn.commit()

    def load_node(self, node_id: str) -> Optional[GraphNode]:
        row = self._conn().execute(
            "SELECT * FROM graph_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        return self._row_to_node(row) if row else None

    def delete_node(self, node_id: str) -> bool:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM graph_nodes WHERE id = ?", (node_id,))
            conn.execute(
                "DELETE FROM graph_links WHERE source_id = ? OR target_id = ?",
                (node_id, node_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_nodes(self, node_type: Optional[NodeType] = None) -> List[GraphNode]:
        if node_type:
            rows = self._conn().execute(
                "SELECT * FROM graph_nodes WHERE type = ?", (node_type.value,)
            ).fetchall()
        else:
            rows = self._conn().execute("SELECT * FROM graph_nodes").fetchall()
        return [self._row_to_node(row) for row in rows]

    # ── Link operations ──

    def save_link(self, link: GraphLink) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO graph_links
                    (source_id, target_id, link_type, weight, context, created_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, target_id, link_type) DO UPDATE SET
                    weight = excluded.weight,
                    context = excluded.context
                """,
                (
                    link.source_id,
                    link.target_id,
                    link.link_type.value,
                    link.weight,
                    json.dumps(link.context, default=str),
                    link.created_at.isoformat(),
                    link.created_by,
                ),
            )
            conn.commit()

    def list_links(
        self,
        node_id: str,
        direction: str = "out",
        link_type: Optional[str] = None,
    ) -> List[GraphLink]:
        conn = self._conn()
        if direction == "out":
            sql = "SELECT * FROM graph_links WHERE source_id = ?"
            params = (node_id,)
        elif direction == "in":
            sql = "SELECT * FROM graph_links WHERE target_id = ?"
            params = (node_id,)
        else:  # 'both'
            sql = "SELECT * FROM graph_links WHERE source_id = ? OR target_id = ?"
            params = (node_id, node_id)
        if link_type:
            sql += " AND link_type = ?"
            params = params + (link_type,)
        rows = conn.execute(sql, params).fetchall()
        return [self._row_to_link(row) for row in rows]

    def delete_links_for_node(self, node_id: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM graph_links WHERE source_id = ? OR target_id = ?",
                (node_id, node_id),
            )
            conn.commit()
            return cur.rowcount

    # ── Bulk operations ──

    def save_graph(self, graph: "MultiLinkGraph") -> int:
        """Save entire graph atomically in a single transaction."""
        nodes = graph.find_nodes()
        with self._conn() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                for node in nodes:
                    conn.execute(
                        """
                        INSERT INTO graph_nodes (id, type, content, metadata, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(id) DO UPDATE SET
                            type = excluded.type,
                            content = excluded.content,
                            metadata = excluded.metadata,
                            updated_at = excluded.updated_at
                        """,
                        (
                            node.id, node.type.value,
                            json.dumps(node.content, default=str),
                            json.dumps(node.metadata, default=str),
                            node.created_at.isoformat(),
                            node.updated_at.isoformat(),
                        ),
                    )
                for source_id, links in graph._outgoing.items():
                    for link in links:
                        conn.execute(
                            """
                            INSERT INTO graph_links
                                (source_id, target_id, link_type, weight, context, created_at, created_by)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(source_id, target_id, link_type) DO UPDATE SET
                                weight = excluded.weight,
                                context = excluded.context
                            """,
                            (
                                link.source_id, link.target_id, link.link_type.value,
                                link.weight,
                                json.dumps(link.context, default=str),
                                link.created_at.isoformat(),
                                link.created_by,
                            ),
                        )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"SQLiteBackend.save_graph failed: {e}")
                raise
        logger.info(f"SQLiteBackend: saved {len(nodes)} nodes")
        return len(nodes)

    def load_graph(self, graph: "MultiLinkGraph") -> int:
        """Load all nodes + links into the in-memory graph."""
        nodes = self.list_nodes()
        for node in nodes:
            graph.add_node(node)

        rows = self._conn().execute("SELECT * FROM graph_links").fetchall()
        for row in rows:
            link = self._row_to_link(row)
            graph._outgoing[link.source_id].append(link)
            graph._incoming[link.target_id].append(link)
            graph._type_index.setdefault(link.link_type, set()).add(link.source_id)
        logger.info(f"SQLiteBackend: loaded {len(nodes)} nodes, {len(rows)} links")
        return len(nodes)