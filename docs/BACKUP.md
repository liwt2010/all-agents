# PR-13: Backup & Restore + Replication Check

## Status: DONE (this commit)

## Goal
Production-grade data safety — add automated backup of all storage backends
(graph + audit logs + custom agents + task store), with restore CLI, retention,
and pre-restore integrity check.

## What's Already There (不重写)
- `migration/engine.py` — data migration between tenants (NOT backup, but reuses snapshot/rollback ideas)
- `memory/storage/` — JSON / SQLite / PostgreSQL backends (from PR-9)
- `core/audit_logger.py` — `BatchAuditLogger` writes JSONL files (PR-11)
- `agents/custom/` — file-based custom agent storage
- `storage/task_store.py` — task records

## Gaps Fixed in This PR

| Gap | Fix |
|-----|-----|
| 没有自动 backup — 数据丢了找不回 | 加 `BackupScheduler` (asyncio 后台任务,默认每天 02:00 跑) |
| 没有 restore 流程 | 加 `restore.py` CLI (从 .tar.gz 还原 + integrity check) |
| backup 写在哪里？磁盘满怎么办 | 加 retention (默认保留 7 天) + 可选 remote destination (S3 / SSH / 本地) |
| backup 是否完整？ | 加 checksum (SHA-256) + 写入 manifest |
| 灾难场景演练 | 加 `drill.py` 模拟 "主库挂了切备份" |

## Backup Strategy

```
Every backup produces:
  backup-YYYYMMDD-HHMMSS.tar.gz
  ├── manifest.json          ← what was backed up + checksums
  ├── graph/                 ← MultiLinkGraph snapshot (PR-9 backend format)
  │   ├── nodes/...          ← or pg_dump output
  │   └── links/...          ← or table dump
  ├── audit/                 ← audit-*.jsonl (date range)
  ├── custom-agents/         ← agents/custom/*.py + .yaml metadata
  ├── tasks/                 ← task_store snapshot
  └── metadata.json          ← backup name, timestamp, version, source backend

Manifest fields:
  {
    "backup_id": "backup-20260707-020000",
    "created_at": "2026-07-07T02:00:00Z",
    "version": "0.1.0",
    "backend": "postgres" | "sqlite" | "json",
    "components": {
      "graph": {"node_count": 1234, "link_count": 5678, "sha256": "abc..."},
      "audit": {"file_count": 30, "total_bytes": 12345678, "sha256": "def..."},
      "custom_agents": {"file_count": 5, "sha256": "..."},
      "tasks": {"record_count": 50, "sha256": "..."}
    },
    "compression": "gzip",
    "size_bytes": 9876543,
    "duration_seconds": 12.3
  }
```

## Backup Sources by Backend

| Backend | Backup method |
|---------|--------------|
| JSON | tar the directory tree |
| SQLite | `VACUUM INTO` (atomic snapshot) then tar the file |
| PostgreSQL | `pg_dump --format=custom` → single file |

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│ BackupScheduler (asyncio task)                           │
│   Schedule: cron-like (daily @ 02:00, hourly if needed) │
│                                                          │
│   On trigger:                                            │
│     1. Snapshot each backend (atomic per-backend)       │
│     2. Compute SHA-256 per component                    │
│     3. tar.gz all components + manifest.json             │
│     4. Write to AGENT_BACKUP_DIR (default ./data/backup) │
│     5. Apply retention (delete backups > N days)         │
│     6. Optional: copy to remote (S3 / SSH)               │
│     7. Audit log: backup completed                       │
└──────────────────────────────────────────────────────────┘

Restore CLI:
  python -m agent_system.core.backup.restore \\
      --from ./data/backup/backup-20260707-020000.tar.gz \\
      --target-backend sqlite --target-path ./data/restored.db \\
      --verify-before-restore    (default true)
```

## Implementation

### Files

| File | Change |
|------|--------|
| `core/backup/__init__.py` | **新增** |
| `core/backup/manifest.py` | **新增** — `BackupManifest` Pydantic model + checksums |
| `core/backup/scheduler.py` | **新增** — `BackupScheduler` async loop |
| `core/backup/sources.py` | **新增** — per-backend snapshot functions |
| `core/backup/restore.py` | **新增** — restore CLI |
| `core/backup/retention.py` | **新增** — cleanup old backups |
| `core/backup/drill.py` | **新增** — disaster recovery drill |
| `api/server.py` | 加 lifespan hook 启动/停止 scheduler |
| `tests/test_backup.py` | **新增** |
| `tests/test_restore.py` | **新增** |

### Configuration

```python
class BackupConfig(BaseModel):
    enabled: bool = True
    schedule_cron: str = "0 2 * * *"          # 02:00 daily (cron syntax)
    backup_dir: str = "./data/backup"
    retention_days: int = 7
    include_graph: bool = True
    include_audit: bool = True
    include_custom_agents: bool = True
    include_tasks: bool = True
    audit_retention_days: int = 7             # only backup audit logs newer than this
    compression: str = "gzip"                 # gzip | none
    remote_destination: Optional[str] = None  # s3://... or ssh://user@host/path
```

环境变量:
- `AGENT_BACKUP_ENABLED=false`
- `AGENT_BACKUP_SCHEDULE_CRON="0 */6 * * *"`  (every 6 hours)
- `AGENT_BACKUP_RETENTION_DAYS=30`

## Disaster Recovery Drill

```bash
python -m agent_system.core.backup.drill \\
    --from ./data/backup/backup-20260707-020000.tar.gz \\
    --target-dir ./data/drill-restore/ \\
    --verify-queries            # run smoke tests against restored data
```

Output:
```
=== DR Drill Report ===
Backup: backup-20260707-020000.tar.gz
Source: postgres @ prod-db:5432
Target: sqlite @ ./data/drill-restore/

Step 1: Extract ............................ OK (12.3s)
Step 2: Verify manifest checksum ........... OK (all 4 components)
Step 3: Load graph into SQLite ............. OK (1234 nodes, 5678 links)
Step 4: Verify query API returns data ...... OK (10 sample queries all match)
Step 5: Audit log search ................... OK (entries from 2026-07-01 to 2026-07-07)
Step 6: Custom agents loadable ............. OK (5 agents found)

Result: PASS — backup is restorable in 12.3s
```

## Test Plan

| Test | Coverage |
|------|----------|
| `test_backup_json_creates_tarball` | JSON backend → tar.gz |
| `test_backup_sqlite_uses_vacuum_into` | SQLite → atomic snapshot |
| `test_backup_postgres_uses_pg_dump` | Postgres → pg_dump (mocked) |
| `test_manifest_includes_checksums` | SHA-256 per component |
| `test_manifest_size_and_duration` | Wall clock measured |
| `test_scheduler_runs_at_scheduled_time` | Cron-like trigger |
| `test_scheduler_retention_purges_old` | 7-day retention |
| `test_restore_extracts_tarball` | Restore JSON |
| `test_restore_to_sqlite_roundtrip` | Backup → restore → query |
| `test_restore_verifies_checksums` | Corrupted manifest → fail |
| `test_drill_passes_for_valid_backup` | DR drill end-to-end |
| `test_drill_fails_on_corrupted_backup` | Tampered tarball caught |
| `test_disabled_no_backup_created` | enabled=False → no work |
| `test_concurrent_backup_blocked` | Two backups can't run simultaneously |

## Out of Scope (deferred)
- Incremental backups (only full backups in this PR)
- WAL archiving for Postgres (point-in-time recovery)
- Backup encryption at rest
- Cross-region replication (caller responsibility to copy to remote)
- Backup monitoring (caller can scrape backup_dir size via Prometheus)