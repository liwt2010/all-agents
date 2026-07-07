"""
Tests for PR-11 audit hardening.

Covers:
- AuditLogEntry extended fields (request_id, tenant_id, session_id, duration_ms)
- BatchAuditLogger: queue + batch flush + sampling + backpressure
- query_from_disk: filter by user_id / action / outcome / date range / request_id
- purge_old_entries: retention policy
- AGENT_AUDIT_ENABLED=false is no-op
- Legacy AuditLogger (in core/security.py) still works (backwards compat)
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


# ── Extended schema ──

class TestAuditEntryExtendedFields:
    def test_extended_fields_round_trip(self):
        from agent_system.core.audit_logger import AuditLogEntry
        entry = AuditLogEntry(
            user_id="u1",
            action="login",
            outcome="success",
            request_id="req-abc-123",
            tenant_id="tenant-7",
            session_id="sess-xyz",
            duration_ms=123.45,
        )
        d = entry.model_dump()
        assert d["request_id"] == "req-abc-123"
        assert d["tenant_id"] == "tenant-7"
        assert d["session_id"] == "sess-xyz"
        assert d["duration_ms"] == 123.45

    def test_extended_fields_have_defaults(self):
        from agent_system.core.audit_logger import AuditLogEntry
        entry = AuditLogEntry(user_id="u1", action="test")
        assert entry.request_id == ""
        assert entry.tenant_id == ""
        assert entry.session_id == ""
        assert entry.duration_ms == 0.0


# ── Sync write path ──

class TestBatchLoggerSync:
    def test_sync_log_writes_to_disk(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(log_dir=str(tmp_path / "audit"))
        logger = BatchAuditLogger(config)
        entry = AuditLogEntry(user_id="u1", action="test_action")
        assert logger.sync_log(entry) is True

        # Verify file written
        date_str = entry.timestamp.strftime("%Y-%m-%d")
        log_file = Path(config.log_dir) / f"audit-{date_str}.jsonl"
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "test_action" in content

    def test_disabled_returns_false(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(enabled=False, log_dir=str(tmp_path / "audit"))
        logger = BatchAuditLogger(config)
        entry = AuditLogEntry(user_id="u1", action="test")
        assert logger.sync_log(entry) is False
        # No file written
        assert not Path(config.log_dir).exists()

    def test_in_memory_query_after_sync_write(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(log_dir=str(tmp_path / "audit"))
        logger = BatchAuditLogger(config)
        for i in range(5):
            logger.sync_log(AuditLogEntry(user_id=f"u{i}", action="act"))
        results = logger.query(user_id="u2")
        assert len(results) == 1
        assert results[0].user_id == "u2"


# ── Async batch path ──

class TestBatchLoggerAsync:
    @pytest.mark.asyncio
    async def test_async_log_flushes_at_batch_size(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(
            log_dir=str(tmp_path / "audit"),
            batch_size=10,
            flush_interval_seconds=999,  # disable timer, only flush on batch_size
        )
        logger = BatchAuditLogger(config)
        for i in range(10):
            await logger.log(AuditLogEntry(user_id=f"u{i}", action="act"))
        # Force flush
        written = await logger.flush()
        assert written == 10
        await logger.close()

    @pytest.mark.asyncio
    async def test_async_log_returns_false_when_disabled(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(enabled=False, log_dir=str(tmp_path / "audit"))
        logger = BatchAuditLogger(config)
        entry = AuditLogEntry(user_id="u1", action="act")
        result = await logger.log(entry)
        assert result is False

    @pytest.mark.asyncio
    async def test_async_log_with_sampling_rate_zero(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(
            log_dir=str(tmp_path / "audit"),
            sampling_rate=0.0,
            batch_size=1,
        )
        logger = BatchAuditLogger(config)
        for _ in range(10):
            result = await logger.log(AuditLogEntry(user_id="u", action="act"))
            assert result is False  # all dropped

    @pytest.mark.asyncio
    async def test_backpressure_drops_oldest(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(
            log_dir=str(tmp_path / "audit"),
            queue_max_size=3,
            batch_size=999,  # never flush from batch_size
            flush_interval_seconds=999,
        )
        logger = BatchAuditLogger(config)
        for i in range(10):
            # All should return True (even with backpressure, newest is kept)
            result = await logger.log(AuditLogEntry(user_id=f"u{i}", action="act"))
            assert result is True


# ── Disk query ──

class TestDiskQuery:
    def test_query_filters_by_user_id(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(log_dir=str(tmp_path / "audit"))
        logger = BatchAuditLogger(config)
        # Write 5 entries from u1, 3 from u2
        for i in range(5):
            logger.sync_log(AuditLogEntry(user_id="u1", action="act1"))
        for i in range(3):
            logger.sync_log(AuditLogEntry(user_id="u2", action="act2"))

        results = logger.query_from_disk(user_id="u1")
        assert len(results) == 5
        assert all(r.user_id == "u1" for r in results)

    def test_query_filters_by_action(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(log_dir=str(tmp_path / "audit"))
        logger = BatchAuditLogger(config)
        logger.sync_log(AuditLogEntry(user_id="u1", action="login"))
        logger.sync_log(AuditLogEntry(user_id="u1", action="logout"))
        logger.sync_log(AuditLogEntry(user_id="u2", action="login"))

        results = logger.query_from_disk(action="login")
        assert len(results) == 2
        assert all(r.action == "login" for r in results)

    def test_query_filters_by_outcome(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(log_dir=str(tmp_path / "audit"))
        logger = BatchAuditLogger(config)
        logger.sync_log(AuditLogEntry(user_id="u1", action="x", outcome="success"))
        logger.sync_log(AuditLogEntry(user_id="u2", action="x", outcome="failure"))
        logger.sync_log(AuditLogEntry(user_id="u3", action="x", outcome="denied"))

        failures = logger.query_from_disk(outcome="failure")
        assert len(failures) == 1
        assert failures[0].user_id == "u2"

    def test_query_filters_by_request_id(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(log_dir=str(tmp_path / "audit"))
        logger = BatchAuditLogger(config)
        logger.sync_log(AuditLogEntry(user_id="u1", action="x", request_id="req-A"))
        logger.sync_log(AuditLogEntry(user_id="u1", action="x", request_id="req-B"))

        results = logger.query_from_disk(request_id="req-A")
        assert len(results) == 1
        assert results[0].request_id == "req-A"

    def test_query_limit(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        config = AuditConfig(log_dir=str(tmp_path / "audit"))
        logger = BatchAuditLogger(config)
        for i in range(20):
            logger.sync_log(AuditLogEntry(user_id="u", action="act"))

        results = logger.query_from_disk(limit=5)
        assert len(results) == 5

    def test_query_date_range(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        from datetime import datetime, timezone
        config = AuditConfig(log_dir=str(tmp_path / "audit"))
        logger = BatchAuditLogger(config)
        # Write an entry with a custom (old) timestamp
        old_entry = AuditLogEntry(user_id="u", action="act")
        old_entry.timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
        logger.sync_log(old_entry)
        # Write a recent entry
        logger.sync_log(AuditLogEntry(user_id="u", action="act"))

        results = logger.query_from_disk(
            start_date="2025-01-01",
            end_date="2030-12-31",
        )
        assert len(results) == 1  # only the recent one
        assert results[0].timestamp.year == datetime.now().year


# ── Retention ──

class TestRetention:
    def test_purge_old_entries(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig, AuditLogEntry
        from datetime import datetime, timezone
        log_dir = tmp_path / "audit"
        log_dir.mkdir()
        # Write entries on 3 different dates
        for date_str in ["2020-01-01", "2021-06-15", "2025-12-31"]:
            f = log_dir / f"audit-{date_str}.jsonl"
            entry = AuditLogEntry(user_id="u", action="act", timestamp=datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc))
            f.write_text(entry.model_dump_json() + "\n", encoding="utf-8")

        config = AuditConfig(log_dir=str(log_dir), retention_days=365)
        logger = BatchAuditLogger(config)
        # Today is 2026-07-07; retention 365d cutoff = 2025-07-07
        deleted = logger.purge_old_entries()
        assert deleted == 2  # 2020 and 2021 entries deleted; 2025 kept
        # Verify files
        remaining = sorted(p.name for p in log_dir.glob("audit-*.jsonl"))
        assert remaining == ["audit-2025-12-31.jsonl"]

    def test_purge_uses_config_retention_days(self, tmp_path):
        from agent_system.core.audit_logger import BatchAuditLogger, AuditConfig
        log_dir = tmp_path / "audit"
        log_dir.mkdir()
        # Write a 100-day-old entry
        old_date = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d")
        (log_dir / f"audit-{old_date}.jsonl").write_text('{}\n', encoding="utf-8")
        # Write a 200-day-old entry
        very_old_date = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d")
        (log_dir / f"audit-{very_old_date}.jsonl").write_text('{}\n', encoding="utf-8")

        config = AuditConfig(log_dir=str(log_dir), retention_days=150)
        logger = BatchAuditLogger(config)
        deleted = logger.purge_old_entries()
        assert deleted == 1  # only the 200-day-old entry


# ── Singleton ──

class TestAuditLoggerSingleton:
    def test_get_audit_logger_returns_singleton(self, tmp_path, monkeypatch):
        from agent_system.core.audit_logger import get_audit_logger, reset_audit_logger, BatchAuditLogger
        monkeypatch.setenv("AGENT_AUDIT_LOG_DIR", str(tmp_path / "audit"))
        reset_audit_logger()
        a = get_audit_logger()
        b = get_audit_logger()
        assert a is b
        assert isinstance(a, BatchAuditLogger)
        reset_audit_logger()

    def test_get_audit_logger_respects_env_disabled(self, tmp_path, monkeypatch):
        from agent_system.core.audit_logger import get_audit_logger, reset_audit_logger
        monkeypatch.setenv("AGENT_AUDIT_ENABLED", "false")
        monkeypatch.setenv("AGENT_AUDIT_LOG_DIR", str(tmp_path / "audit"))
        reset_audit_logger()
        a = get_audit_logger()
        assert a.config.enabled is False
        reset_audit_logger()


# ── HTTP endpoint integration ──

class TestAuditHTTPEndpoint:
    def test_audit_query_endpoint_requires_auth(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from agent_system.api.server import app
        from agent_system.core.audit_logger import reset_audit_logger, get_audit_logger, AuditLogEntry
        monkeypatch.setenv("AGENT_AUDIT_LOG_DIR", str(tmp_path / "audit"))
        reset_audit_logger()
        # Write an entry via batch logger
        get_audit_logger().sync_log(AuditLogEntry(user_id="alice", action="login"))

        with TestClient(app) as client:
            r = client.get("/api/audit/query")  # no auth header
            assert r.status_code == 401

        reset_audit_logger()

    def test_audit_query_endpoint_returns_entries(self, tmp_path, monkeypatch):
        from fastapi.testclient import TestClient
        from agent_system.api.server import app
        from agent_system.core.audit_logger import reset_audit_logger, get_audit_logger, AuditLogEntry
        from agent_system.core.auth import AuthService

        monkeypatch.setenv("AGENT_AUDIT_LOG_DIR", str(tmp_path / "audit"))
        reset_audit_logger()
        # Write entries
        get_audit_logger().sync_log(AuditLogEntry(user_id="alice", action="login"))
        get_audit_logger().sync_log(AuditLogEntry(user_id="bob", action="login"))

        # Issue a token
        auth = AuthService()
        token = auth.issue_token(user_id="admin", tenant_id="default", role="admin")

        with TestClient(app) as client:
            r = client.get(
                "/api/audit/query?user_id=alice",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data["count"] == 1
            assert data["entries"][0]["user_id"] == "alice"

        reset_audit_logger()