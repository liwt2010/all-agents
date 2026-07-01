"""
Tests: Async audit logger, redaction, log rotation
"""

import asyncio
import pytest
from datetime import datetime

from agent_system.core.audit_logger import (
    AuditLogger,
    AuditLogEntry,
    redact,
    configure_logger,
)


class TestRedact:
    def test_api_key_redacted(self):
        result = redact("My API key is sk-1234567890abcdef")
        assert "sk-1234567890abcdef" not in result
        assert "***SK***" in result

    def test_email_redacted(self):
        result = redact("contact me at alice@example.com")
        assert "@" not in result
        assert "***EMAIL***" in result

    def test_jwt_redacted(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.abc123"
        result = redact(f"token = {jwt}")
        assert "eyJ" not in result
        assert "***JWT***" in result

    def test_normal_text_not_affected(self):
        result = redact("The agent completed the task successfully")
        assert "agent" in result

    def test_credit_card_redacted(self):
        result = redact("card 4111111111111111 expired")
        assert "***CC***" in result

    def test_long_text_truncated(self):
        long_text = "x" * 3000
        result = redact(long_text)
        assert len(result) <= 2000

    def test_bearer_token_redacted(self):
        result = redact("Authorization: Bearer some-secret-token")
        assert "***BEARER***" in result


class TestAuditLogger:
    @pytest.mark.asyncio
    async def test_log_async_non_blocking(self, tmp_path):
        """Should write without error and not block the event loop."""
        logger = AuditLogger(str(tmp_path))
        entry = AuditLogEntry(
            user_id="alice",
            action="task.run",
            resource_id="t-1",
        )
        result = await logger.log(entry)
        assert result is True

        # In-memory store
        entries = logger.query(user_id="alice")
        assert len(entries) == 1
        assert entries[0].action == "task.run"

    @pytest.mark.asyncio
    async def test_query_by_action(self, tmp_path):
        logger = AuditLogger(str(tmp_path))
        await logger.log(AuditLogEntry(user_id="u1", action="task.run", resource_id="t1"))
        await logger.log(AuditLogEntry(user_id="u1", action="task.rejected", resource_id="t2"))

        results = logger.query(action="task.rejected")
        assert len(results) == 1

    def test_sync_write_fallback(self, tmp_path):
        """Should work without an event loop."""
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            pytest.skip("Event loop is running")
        except RuntimeError:
            pass
        logger = AuditLogger(str(tmp_path))
        entry = AuditLogEntry(user_id="sys", action="sync.write", resource_id="test")
        # sync_log should write without raising
        assert logger.sync_log(entry) is True

    @pytest.mark.asyncio
    async def test_multiple_entries_persist_in_order(self, tmp_path):
        logger = AuditLogger(str(tmp_path))
        for i in range(5):
            await logger.log(AuditLogEntry(user_id="u1", action=f"event-{i}", resource_id=f"r{i}"))
        results = logger.query(user_id="u1", limit=10)
        assert len(results) == 5
        assert results[0].action == "event-0"
        assert results[4].action == "event-4"


class TestConfigureLogger:
    def test_logger_has_rotating_handler(self, tmp_path):
        log = configure_logger("test_rotate", log_dir=str(tmp_path / "logs"))
        handlers = log.handlers
        from logging.handlers import RotatingFileHandler
        assert any(isinstance(h, RotatingFileHandler) for h in handlers)
