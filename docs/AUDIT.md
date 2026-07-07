# PR-11: Audit Logger 加固

## Status: DONE (this commit)

## Goal
生产化 Audit 层 — 既有 `core/audit_logger.py` 有 JSONL 文件 + 异步写 + rotation,本 PR 加:
1. **Batch queue** — 高吞吐场景下批量写盘 (避免每事件一次 syscall)
2. **Retention policy** — 自动清理 N 天前的审计文件
3. **结构化查询 API** — HTTP 端点支持时间范围 / 用户 / action / outcome 过滤
4. **extended schema** — 增加 `request_id` / `tenant_id` / `session_id` 字段
5. **Sampling** — 默认全量,生产可配置 sampling rate (1.0 = 100%, 0.1 = 10%)

## What's Already There (不重写)
- `core/audit_logger.py` — `AuditLogEntry` 模型 + `AuditLogger` 类
- `core/audit/query.py` — 已有查询逻辑
- `core/security.py:264` — global `audit_logger` 实例
- `api/server.py` — 4 个 endpoint 调用 `audit_logger.log(AuditLogEntry(...))`

## Gaps Fixed in This PR

| Gap | Fix |
|-----|-----|
| 每事件一次 write → 高并发下 IO 瓶颈 | 加 `BatchAuditLogger` queue + background flush |
| 没有 retention policy → 日志无限增长 | 加 `purge_old_entries(retention_days)` |
| 查询只能 in-memory → 重启后丢历史 | 加 `query_from_disk(start_date, end_date, ...)` |
| AuditLogEntry 缺 request_id / tenant_id | schema 扩展 (向后兼容,默认空字符串) |
| 没有 sampling 配置 | 加 `AuditConfig(sampling_rate=1.0, batch_size=100, flush_interval=5.0)` |
| 没有 HTTP 查询 endpoint | 加 `GET /api/audit/query` 受 admin scope 保护 |

## Extended Schema

```python
class AuditLogEntry(BaseModel):
    # Existing (保持向后兼容)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    user_id: str = ""
    action: str = ""
    resource_id: str = ""
    resource_type: str = ""
    details: Dict[str, Any] = Field(default_factory=dict)
    ip_address: str = ""
    user_agent: str = ""
    outcome: str = "success"

    # NEW (PR-11)
    request_id: str = ""          # from RequestIDMiddleware (PR-7)
    tenant_id: str = ""           # multi-tenant isolation
    session_id: str = ""          # user session correlation
    duration_ms: float = 0.0      # action execution time
```

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ audit_logger.log(entry)                                             │
└─────┬───────────────────────────────────────────────────────────────┘
      ▼
┌──────────────────────────────────────┐
│ AuditConfig check:                   │
│   - sampling_rate: drop if random>N  │
│   - enabled: no-op if False          │
└─────┬─────────────────────────────────┘
      ▼
┌──────────────────────────────────────┐
│ Queue (asyncio.Queue, maxsize=10000) │
│   - put_nowait (non-blocking)        │
│   - backpressure: drop oldest if full│
└─────┬─────────────────────────────────┘
      ▼
┌──────────────────────────────────────┐
│ Background task (BatchedAuditLogger) │
│   - batch_size=100 OR                │
│   - flush_interval=5.0s              │
│   - asyncio.to_thread(write_batch)   │
└─────┬─────────────────────────────────┘
      ▼
┌──────────────────────────────────────┐
│ data/audit/audit-YYYY-MM-DD.jsonl    │
│   RotatingFileHandler 10MB × 10      │
└──────────────────────────────────────┘

Query path:
GET /api/audit/query?user_id=&action=&start=&end=&outcome=&limit=
  → scan audit-*.jsonl files in date range
  → filter
  → return paginated list
```

## Implementation

### Files

| File | Change |
|------|--------|
| `src/agent_system/core/audit_logger.py` | 扩展 AuditLogEntry + 新增 `BatchAuditLogger` + `AuditConfig` |
| `src/agent_system/core/audit/query.py` | 扩展支持 disk scan + retention |
| `src/agent_system/api/server.py` | 加 `GET /api/audit/query` endpoint (admin scope) |
| `src/agent_system/core/security.py` | 把 `audit_logger` 替换为 `BatchAuditLogger` |
| `src/agent_system/core/request_id.py` | 把 `get_request_id()` 注入 AuditLogEntry (PR-7 已有 contextvar) |
| `tests/test_audit_batch.py` | **新增** — batch / retention / query 测试 |

### Configuration

```python
# config/settings.py
class AuditConfig(BaseModel):
    enabled: bool = True
    sampling_rate: float = 1.0
    batch_size: int = 100
    flush_interval_seconds: float = 5.0
    retention_days: int = 90
    log_dir: str = "./data/audit"
    queue_max_size: int = 10000
```

环境变量覆盖:
- `AGENT_AUDIT_ENABLED=false` — 关闭 audit
- `AGENT_AUDIT_SAMPLING_RATE=0.1` — 10% 采样
- `AGENT_AUDIT_RETENTION_DAYS=30`

### Migration Path

`AuditLogger` 旧类保留为 compatibility shim:
- `audit_logger.log(entry)` 直接走 legacy sync write
- 新代码用 `BatchAuditLogger` 实例 (via `get_audit_logger()`)
- 既有 4 个 endpoint 不变 (它们调 `audit_logger.log(...)`)

## Test Plan

| Test | Coverage |
|------|----------|
| `test_audit_entry_extended_fields` | request_id / tenant_id / session_id round-trip |
| `test_batch_logger_drops_when_queue_full` | backpressure |
| `test_batch_logger_flushes_at_batch_size` | 100 entries → 1 write call |
| `test_batch_logger_flushes_at_interval` | 5s timer triggers flush |
| `test_sampling_drops_entries` | sampling_rate=0 → no writes |
| `test_query_from_disk_by_date_range` | scan 7 days of audit |
| `test_query_filter_by_user_action_outcome` | 4 filter combinations |
| `test_purge_old_entries` | retention=30d → files >30d deleted |
| `test_audit_disabled_is_noop` | enabled=False → queue not even created |
| `test_legacy_audit_logger_still_works` | backwards compat |

## Performance Impact

| Scenario | Legacy (sync) | PR-11 (batch) | Improvement |
|----------|---------------|---------------|-------------|
| 100 events/sec | 100 writes/s | ~1 write/5s | 500x fewer syscalls |
| 1000 events/sec | 1000 writes/s | ~10 writes/5s | 100x fewer syscalls |
| Latency | write() blocks | put_nowait non-blocking | async event loop 不卡 |

## Out of Scope (deferred to PR-12+)
- Remote audit sink (e.g., Loki / ELK / Splunk) — only local JSONL
- Tamper-evident hashing (blockchain-style chain)
- Per-tenant encryption at rest
- Real-time alerting on critical actions (e.g., "5 failed logins in 1 min")