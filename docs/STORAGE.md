# Storage Backend — 设计文档 (PR 9)

> 把 JSON 文件持久化换成可插拔 storage backend。

## 1. 背景与目标

**问题**：当前 `memory/persistence.py` 把 `MultiLinkGraph` 存到 JSON 文件 (`data/graph/nodes/{type}/{id}.json`)。这在 demo / 培训场景够用，但**生产环境**不可接受：

- ❌ 单进程写入，并发读写会丢数据
- ❌ 无索引，全表扫描
- ❌ 无事务，半写入状态无法回滚
- ❌ 无备份机制（只能手工拷贝目录）
- ❌ 无跨实例协调（多副本会冲突）

**目标**：引入可插拔 storage 抽象，**生产环境**使用 PostgreSQL（写）+ Redis（读/缓存），**开发环境**保留 SQLite（单文件，零运维），**迁移场景**保留 JSON（import/export）。

## 2. 借鉴 + 创新

### 借鉴自 UAMS (Universal Agent Memory)

- 6 个 backend 的统一接口（InMemory/SQLite/PostgreSQL/Redis/Neo4j/ChromaDB）
- 连接池（PostgreSQL）
- 配置驱动 backend 选择（env var）

### all-agents 自身优势（保留不动）

- `MultiLinkGraph` 内存主存是 source of truth，**只**持久化层换
- **11 节点类型 + 23 链接类型**（比 UAMS 丰富）
- `Dataview` SQL 引擎（PR 1）已经提供 graph 检索能力

### 设计原则

1. **渐进切换**：JSON 仍然作为 import/export 格式（迁移路径），但运行时只走 SQLite/PostgreSQL/Redis
2. **多后端可叠加**：写主存 + 读缓存可不同（如 PostgreSQL 写 + Redis 读）
3. **零中断切换**：默认后端从 JSON 改 SQLite，对调用方零影响

## 3. 架构

```
                       ┌─────────────────────┐
                       │  MultiLinkGraph     │  ← 内存主存 (source of truth)
                       │  (in-process)       │
                       └──────────┬──────────┘
                                  │ save_node / load_node
                                  ▼
                       ┌─────────────────────┐
                       │  GraphStorage       │  ← 抽象接口
                       │  (Protocol)         │
                       └──────────┬──────────┘
                                  │
        ┌──────────┬──────────────┼──────────────┐
        │          │              │              │
        ▼          ▼              ▼              ▼
    ┌──────┐  ┌────────┐  ┌──────────┐  ┌──────────┐
    │ JSON │  │ SQLite │  │PostgreSQL│  │  Redis   │
    │ (IO) │  │ (file) │  │ (server) │  │ (cache)  │
    └──────┘  └────────┘  └──────────┘  └──────────┘
   迁移/导出   开发/单机     生产主存      生产读缓存
```

## 4. 接口设计

```python
# memory/storage/base.py
from typing import Protocol, List, Optional
from agent_system.memory.graph import GraphNode, GraphLink, NodeType, LinkType


class GraphStorage(Protocol):
    """Storage backend interface for MultiLinkGraph nodes and links."""

    # ── Node operations ──
    def save_node(self, node: GraphNode) -> None: ...
    def load_node(self, node_id: str) -> Optional[GraphNode]: ...
    def delete_node(self, node_id: str) -> bool: ...
    def list_nodes(self, node_type: Optional[NodeType] = None) -> List[GraphNode]: ...

    # ── Link operations ──
    def save_link(self, link: GraphLink) -> None: ...
    def list_links(self, node_id: str, direction: str = "out") -> List[GraphLink]: ...

    # ── Bulk operations ──
    def save_graph(self, graph: "MultiLinkGraph") -> int:
        """Save entire graph atomically. Returns count saved."""
        ...

    def load_graph(self, graph: "MultiLinkGraph") -> int:
        """Load all nodes + links into graph. Returns count loaded."""
        ...

    # ── Lifecycle ──
    def init(self) -> None:
        """Initialize schema (tables, indexes). Idempotent."""
        ...

    def close(self) -> None:
        """Release connections."""
        ...
```

## 5. Backend 实现

### 5.1 JSON (current — 保留为 import/export)

- 不变，**只是包一层适配**
- 用于：迁移、备份、调试

### 5.2 SQLite (new — 默认开发环境)

- 单文件 `data/graph.db`，WAL 模式支持并发读
- 索引：`idx_nodes_type`, `idx_links_source`, `idx_links_target`
- 事务：BEGIN/COMMIT 包整图保存

### 5.3 PostgreSQL (new — 生产主存)

- 连接池：`min=2, max=20` (env override)
- 同样 schema + 索引
- JSONB 字段支持快速查询 content/metadata 嵌套键

### 5.4 Redis (PR-10 — 后续)

- 用作 L2 缓存层，**不**是主存
- 降低 PostgreSQL 读压力
- 跨实例信号共享

## 6. Schema

### SQL DDL (SQLite / PostgreSQL 共享)

```sql
CREATE TABLE IF NOT EXISTS graph_nodes (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    content         JSONB NOT NULL,        -- PostgreSQL / JSON in SQLite
    metadata        JSONB NOT NULL,
    created_at      TIMESTAMP NOT NULL,
    updated_at      TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_nodes_type ON graph_nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_created_at ON graph_nodes(created_at);

CREATE TABLE IF NOT EXISTS graph_links (
    source_id       TEXT NOT NULL,
    target_id       TEXT NOT NULL,
    link_type       TEXT NOT NULL,
    weight          REAL DEFAULT 1.0,
    context         JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMP NOT NULL,
    created_by      TEXT DEFAULT 'system',
    PRIMARY KEY (source_id, target_id, link_type)
);

CREATE INDEX IF NOT EXISTS idx_links_source ON graph_links(source_id);
CREATE INDEX IF NOT EXISTS idx_links_target ON graph_links(target_id);
CREATE INDEX IF NOT EXISTS idx_links_type ON graph_links(link_type);
```

## 7. 配置

```python
# agent_system/config/settings.py 新增
class StorageConfig(BaseModel):
    backend: str = "sqlite"            # "json" | "sqlite" | "postgresql"
    json_path: str = "./data/graph"     # 旧版 JSON 路径
    sqlite_path: str = "./data/graph.db"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_database: str = "agent_system"
    postgres_user: str = "agent"
    postgres_password: str = ""
    postgres_pool_min: int = 2
    postgres_pool_max: int = 20
```

环境变量（生产部署用）：
```bash
AGENT_STORAGE_BACKEND=postgresql
AGENT_POSTGRES_HOST=db.prod.local
AGENT_POSTGRES_PORT=5432
AGENT_POSTGRES_DATABASE=agent_system
AGENT_POSTGRES_USER=agent
AGENT_POSTGRES_PASSWORD=***
AGENT_POSTGRES_POOL_MAX=20
```

## 8. 兼容性策略

### 8.1 调用方零改动

```python
# 之前
from agent_system.memory.persistence import save_graph, load_graph
save_graph(graph)                    # 旧 API 仍然工作

# 之后 (默认后端从 JSON → SQLite)
save_graph(graph)                    # 自动走 SQLite
```

`persistence.py` 变成 **facade**，内部根据配置选 backend。**不需要改任何调用代码**。

### 8.2 数据迁移

```bash
# 从 JSON 迁到 SQLite（新默认）
python -m agent_system.memory.storage.migrate --from json --to sqlite

# 从 SQLite 迁到 PostgreSQL（生产化）
python -m agent_system.memory.storage.migrate --from sqlite --to postgresql
```

## 9. 测试策略

- 每个 backend 独立单元测试（同一个测试套件，参数化 backend）
- `JSON` ↔ `SQLite` 迁移 round-trip 测试
- `SQLite` ↔ `PostgreSQL` 迁移 round-trip 测试（CI 用 SQLite，生产用 PostgreSQL）
- 性能 baseline：1000 节点 save/load < 1s

## 10. 实现路径

| 步骤 | 文件 | 工作量 |
|---|---|---|
| docs/STORAGE.md | NEW | 本文档 (done) |
| base.py | NEW | GraphStorage Protocol |
| json_backend.py | NEW | 适配现有 JSON 逻辑 |
| sqlite_backend.py | NEW | SQLite 实现 |
| postgres_backend.py | NEW | PostgreSQL 实现（依赖 `psycopg2-binary`，已在 requirements） |
| migrate.py | NEW | 迁移 CLI |
| persistence.py | MODIFIED | 改成 facade |
| settings.py | MODIFIED | 加 StorageConfig |
| tests/test_storage.py | NEW | 6 个测试 × 3 backend = 18+ 测试 |

PR 9 完成后再考虑：
- PR-10 Redis 缓存层
- PR-13 Backup/Migration 工具加固

## 11. 风险与对策

| 风险 | 严重度 | 对策 |
|---|---|---|
| SQLite 并发写竞争 | 中 | WAL 模式 + 事务 |
| PostgreSQL 连接池耗尽 | 中 | pool_max 限流 + 超时配置 |
| 数据迁移失败 | 高 | 迁移前自动 backup + dry-run 模式 |
| 后端 bug 导致数据丢失 | 高 | 持久化层测试覆盖率 90%+ |
| PostgreSQL 依赖不可用 | 低 | graceful fallback 到 SQLite（warning log）|