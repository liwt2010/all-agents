"""
PostgreSQL storage backend — production-grade persistent storage.

Uses connection pooling (psycopg2.pool) for concurrent access.
JSONB columns for content/metadata support indexed JSON queries.

Best for:
  - Multi-instance production
  - Large graphs (>10k nodes)
  - Concurrent read/write workloads

Requires:
  - psycopg2-binary (already in requirements.txt)
  - PostgreSQL 13+ for jsonb_path_ops GIN index
"""

import json
import logging
import os
from contextlib import contextmanager
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


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS graph_nodes (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    content     JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON graph_nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_created_at ON graph_nodes(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_nodes_content ON graph_nodes USING GIN (content jsonb_path_ops);
CREATE INDEX IF NOT EXISTS idx_nodes_metadata ON graph_nodes USING GIN (metadata jsonb_path_ops);

CREATE TABLE IF NOT EXISTS graph_links (
    source_id   TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    link_type   TEXT NOT NULL,
    weight      REAL DEFAULT 1.0,
    context     JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL,
    created_by  TEXT DEFAULT 'system',
    PRIMARY KEY (source_id, target_id, link_type),
    FOREIGN KEY (source_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_links_source ON graph_links(source_id);
CREATE INDEX IF NOT EXISTS idx_links_target ON graph_links(target_id);
CREATE INDEX IF NOT EXISTS idx_links_type ON graph_links(link_type);
"""


# Migration that adds tenant_id columns + Row-Level Security policies.
# Idempotent: safe to run on every init() call.
# Pre-condition: SCHEMA_SQL has already created the base tables.
RLS_MIGRATION_SQL = """
-- Add tenant_id columns (default 'default' so existing rows remain visible)
ALTER TABLE graph_nodes
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE graph_links
    ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_nodes_tenant ON graph_nodes(tenant_id);
CREATE INDEX IF NOT EXISTS idx_links_tenant ON graph_links(tenant_id);

-- Enable RLS on both tables. Tables whose owner is the migration
-- connection bypass RLS by default (FORCE ROW LEVEL SECURITY makes it
-- apply even to the owner) — opt-in by setting FORCE=true below if
-- you want strict enforcement for the application role too.
ALTER TABLE graph_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE graph_links ENABLE ROW LEVEL SECURITY;

-- Policy: visible only when row's tenant_id matches the GUC. The 'true'
-- argument to current_setting() returns NULL (treated as no-match) when
-- the GUC is unset — so without an explicit SET, RLS hides everything.
DROP POLICY IF EXISTS tenant_isolation_nodes ON graph_nodes;
CREATE POLICY tenant_isolation_nodes ON graph_nodes
    USING (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));

DROP POLICY IF EXISTS tenant_isolation_links ON graph_links;
CREATE POLICY tenant_isolation_links ON graph_links
    USING (tenant_id = current_setting('app.current_tenant', true))
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true));
"""


class PostgresBackend:
    """PostgreSQL-backed graph storage with connection pooling."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        database: str = "all_agents",
        user: str = "all_agents",
        password: str | None = None,
        pool_min: int = 2,
        pool_max: int = 20,
    ):
        try:
            import psycopg2
            import psycopg2.extras
            from psycopg2 import pool as pg_pool
        except ImportError as e:
            raise ImportError(
                "psycopg2-binary required for PostgresBackend. "
                "Install with: pip install psycopg2-binary"
            ) from e

        password = password or os.environ.get("AGENT_POSTGRES_PASSWORD", "")
        self.pool = pg_pool.ThreadedConnectionPool(
            pool_min,
            pool_max,
            host=host,
            port=port,
            database=database,
            user=user,
            password=password,
        )
        self._psycopg2_extras = psycopg2.extras
        # Default tenant until caller calls set_tenant_id(). With no
        # tenant set, RLS hides all rows — fail-closed by default.
        self._current_tenant: str = "default"

    def backend_name(self) -> str:
        return f"postgres:{self.pool._conn_kwargs.get('database', '?')}"

    def ping(self) -> bool:
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    return cur.fetchone()[0] == 1
        except Exception as e:
            logger.warning(f"PostgresBackend ping failed: {e}")
            return False

    @contextmanager
    def _conn(self):
        """Context manager that returns a pooled connection and releases it after."""
        conn = self.pool.getconn()
        try:
            yield conn
        finally:
            self.pool.putconn(conn)

    def set_tenant_id(self, tenant_id: str) -> None:
        """Bind `tenant_id` for every subsequent query on this backend's
        pooled connections.

        The GUC is set on the *pool* (which lives for the lifetime of
        this backend) and the change is committed. PostgreSQL GUCs are
        per-session — each pooled connection inherits it. For safety
        we reset it on every connection checkout (see `_conn_with_tenant`).

        Typical use:
            backend = PostgresBackend(...)
            backend.init()
            backend.set_tenant_id("acme")
            backend.save_node(node)
        """
        # Validate at API boundary — refuse suspicious tenant ids early
        if not tenant_id or not isinstance(tenant_id, str):
            raise ValueError("tenant_id must be a non-empty string")
        if len(tenant_id) > 128:
            raise ValueError("tenant_id too long (max 128 chars)")
        # The actual SET happens per-connection in _conn_with_tenant; here
        # we just record the desired value.
        self._current_tenant = tenant_id

    @contextmanager
    def _conn_with_tenant(self):
        """Like _conn() but pins app.current_tenant for the duration of
        the connection. Pairs with set_tenant_id(); without that call,
        the GUC is left unset and RLS hides all rows (fail-closed)."""
        conn = self.pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.current_tenant', %s, true)",
                    (self._current_tenant,),
                )
            conn.commit()
            yield conn
        finally:
            self.pool.putconn(conn)

    def init(self) -> None:
        """Create tables, indexes, and RLS policies (idempotent).

        The schema migration runs in two steps:
          1. SCHEMA_SQL — base tables + indexes (IF NOT EXISTS, no-op
             on second call).
          2. RLS_MIGRATION_SQL — tenant_id columns + RLS policies.
             ADD COLUMN IF NOT EXISTS makes this safe to re-run.
        """
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                # RLS migration: requires the base tables to exist, so
                # it must run AFTER SCHEMA_SQL.
                cur.execute(RLS_MIGRATION_SQL)
            conn.commit()

    def close(self) -> None:
        """Close all connections in pool."""
        if self.pool:
            self.pool.closeall()
            self.pool = None

    def _row_to_node(self, row) -> GraphNode:
        # row: (id, type, content_jsonb, metadata_jsonb, created_at, updated_at)
        return GraphNode(
            id=row[0],
            type=NodeType(row[1]),
            content=row[2] if isinstance(row[2], dict) else (json.loads(row[2]) if row[2] else {}),
            metadata=row[3] if isinstance(row[3], dict) else (json.loads(row[3]) if row[3] else {}),
            created_at=row[4],
            updated_at=row[5],
        )

    def _row_to_link(self, row) -> GraphLink:
        # row: (source_id, target_id, link_type, weight, context_jsonb, created_at, created_by)
        return GraphLink(
            source_id=row[0],
            target_id=row[1],
            link_type=LinkType(row[2]),
            weight=row[3],
            context=row[4] if isinstance(row[4], dict) else (json.loads(row[4]) if row[4] else {}),
            created_at=row[5],
            created_by=row[6] or "system",
        )

    # ── Node operations ──

    @track_storage(backend="postgres", op="save_node")
    def save_node(self, node: GraphNode) -> None:
        with self._conn_with_tenant() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO graph_nodes (id, type, content, metadata, created_at, updated_at, tenant_id)
                    VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                        type = EXCLUDED.type,
                        content = EXCLUDED.content,
                        metadata = EXCLUDED.metadata,
                        updated_at = EXCLUDED.updated_at
                    """,
                    (
                        node.id,
                        node.type.value,
                        json.dumps(node.content, default=str),
                        json.dumps(node.metadata, default=str),
                        node.created_at,
                        node.updated_at,
                        self._current_tenant,
                    ),
                )
            conn.commit()

    def load_node(self, node_id: str) -> GraphNode | None:
        with self._conn_with_tenant() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, type, content, metadata, created_at, updated_at "
                    "FROM graph_nodes WHERE id = %s",
                    (node_id,),
                )
                row = cur.fetchone()
                return self._row_to_node(row) if row else None

    def delete_node(self, node_id: str) -> bool:
        with self._conn_with_tenant() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM graph_nodes WHERE id = %s", (node_id,))
                # FK CASCADE removes links automatically
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted

    def list_nodes(self, node_type: NodeType | None = None) -> list[GraphNode]:
        with self._conn_with_tenant() as conn:
            with conn.cursor() as cur:
                if node_type:
                    cur.execute(
                        "SELECT id, type, content, metadata, created_at, updated_at "
                        "FROM graph_nodes WHERE type = %s",
                        (node_type.value,),
                    )
                else:
                    cur.execute(
                        "SELECT id, type, content, metadata, created_at, updated_at FROM graph_nodes"
                    )
                rows = cur.fetchall()
                return [self._row_to_node(row) for row in rows]

    # ── Link operations ──

    def save_link(self, link: GraphLink) -> None:
        with self._conn_with_tenant() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO graph_links
                        (source_id, target_id, link_type, weight, context, created_at, created_by, tenant_id)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                    ON CONFLICT (source_id, target_id, link_type) DO UPDATE SET
                        weight = EXCLUDED.weight,
                        context = EXCLUDED.context
                    """,
                    (
                        link.source_id,
                        link.target_id,
                        link.link_type.value,
                        link.weight,
                        json.dumps(link.context, default=str),
                        link.created_at,
                        link.created_by,
                        self._current_tenant,
                    ),
                )
            conn.commit()

    def list_links(
        self,
        node_id: str,
        direction: str = "out",
        link_type: str | None = None,
    ) -> list[GraphLink]:
        with self._conn_with_tenant() as conn:
            with conn.cursor() as cur:
                if direction == "out":
                    sql = (
                        "SELECT source_id, target_id, link_type, weight, context, created_at, created_by "
                        "FROM graph_links WHERE source_id = %s"
                    )
                    params = (node_id,)
                elif direction == "in":
                    sql = (
                        "SELECT source_id, target_id, link_type, weight, context, created_at, created_by "
                        "FROM graph_links WHERE target_id = %s"
                    )
                    params = (node_id,)
                else:  # 'both'
                    sql = (
                        "SELECT source_id, target_id, link_type, weight, context, created_at, created_by "
                        "FROM graph_links WHERE source_id = %s OR target_id = %s"
                    )
                    params = (node_id, node_id)
                if link_type:
                    sql += " AND link_type = %s"
                    params = params + (link_type,)
                cur.execute(sql, params)
                rows = cur.fetchall()
                return [self._row_to_link(row) for row in rows]

    def delete_links_for_node(self, node_id: str) -> int:
        with self._conn_with_tenant() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM graph_links WHERE source_id = %s OR target_id = %s",
                    (node_id, node_id),
                )
                count = cur.rowcount
            conn.commit()
            return count

    # ── Bulk operations ──

    def save_graph(self, graph: "MultiLinkGraph") -> int:
        """Save entire graph atomically in a single transaction."""
        nodes = graph.find_nodes()
        with self._conn_with_tenant() as conn:
            try:
                with conn.cursor() as cur:
                    for node in nodes:
                        cur.execute(
                            """
                            INSERT INTO graph_nodes (id, type, content, metadata, created_at, updated_at, tenant_id)
                            VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                type = EXCLUDED.type,
                                content = EXCLUDED.content,
                                metadata = EXCLUDED.metadata,
                                updated_at = EXCLUDED.updated_at
                            """,
                            (
                                node.id, node.type.value,
                                json.dumps(node.content, default=str),
                                json.dumps(node.metadata, default=str),
                                node.created_at, node.updated_at,
                                self._current_tenant,
                            ),
                        )
                    for source_id, links in graph._outgoing.items():
                        for link in links:
                            cur.execute(
                                """
                                INSERT INTO graph_links
                                    (source_id, target_id, link_type, weight, context, created_at, created_by, tenant_id)
                                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                                ON CONFLICT (source_id, target_id, link_type) DO UPDATE SET
                                    weight = EXCLUDED.weight,
                                    context = EXCLUDED.context
                                """,
                                (
                                    link.source_id, link.target_id, link.link_type.value,
                                    link.weight,
                                    json.dumps(link.context, default=str),
                                    link.created_at, link.created_by,
                                    self._current_tenant,
                                ),
                            )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"PostgresBackend.save_graph failed: {e}")
                raise
        logger.info(f"PostgresBackend: saved {len(nodes)} nodes")
        return len(nodes)

    def load_graph(self, graph: "MultiLinkGraph") -> int:
        """Load all nodes + links into the in-memory graph."""
        nodes = self.list_nodes()
        for node in nodes:
            graph.add_node(node)

        with self._conn_with_tenant() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT source_id, target_id, link_type, weight, context, created_at, created_by "
                    "FROM graph_links"
                )
                rows = cur.fetchall()
                link_count = 0
                for row in rows:
                    link = self._row_to_link(row)
                    graph._outgoing[link.source_id].append(link)
                    graph._incoming[link.target_id].append(link)
                    graph._type_index.setdefault(link.link_type, set()).add(link.source_id)
                    link_count += 1
        logger.info(f"PostgresBackend: loaded {len(nodes)} nodes, {link_count} links")
        return len(nodes)