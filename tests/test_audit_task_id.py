"""
Tests for audit task_id (v0.6.0-3).

Covers:
  - AuditLogEntry carries task_id field
  - AuditLogger.query filters by task_id
  - BatchAuditLogger.query filters by task_id
  - Legacy entries (resource_type="task" + resource_id=task_id)
    still match the new task_id filter (fallback path)
"""
from __future__ import annotations

import pytest

from agent_system.core.audit_logger import AuditConfig, AuditLogEntry, BatchAuditLogger


def _entry(action: str, **fields) -> AuditLogEntry:
    return AuditLogEntry(action=action, **fields)


class TestAuditLoggerInMemory:
    def test_task_id_field_round_trips(self):
        e = AuditLogEntry(action="task.claimed", task_id="t-1")
        assert e.task_id == "t-1"

    def test_query_filters_by_task_id(self):
        from agent_system.core.audit_logger import AuditLogger
        al = AuditLogger()
        al._in_memory.extend([
            _entry("task.claimed", task_id="t-1", user_id="alice"),
            _entry("task.claimed", task_id="t-2", user_id="bob"),
            _entry("task.completed", task_id="t-1", user_id="alice"),
        ])
        results = al.query(task_id="t-1")
        assert len(results) == 2
        assert all(e.task_id == "t-1" for e in results)

    def test_legacy_resource_type_fallback(self):
        """Entries written before v0.6.0 used
        resource_type='task' + resource_id=task_id. They should still
        match the new task_id filter."""
        from agent_system.core.audit_logger import AuditLogger
        al = AuditLogger()
        legacy = AuditLogEntry(
            action="task.rejected",
            resource_type="task",
            resource_id="t-1",
            user_id="alice",
        )
        al._in_memory.append(legacy)
        results = al.query(task_id="t-1")
        assert len(results) == 1
        assert results[0].resource_id == "t-1"

    def test_query_combines_task_id_and_action(self):
        from agent_system.core.audit_logger import AuditLogger
        al = AuditLogger()
        al._in_memory.extend([
            _entry("task.claimed", task_id="t-1"),
            _entry("task.completed", task_id="t-1"),
            _entry("task.claimed", task_id="t-2"),
        ])
        results = al.query(task_id="t-1", action="task.claimed")
        assert len(results) == 1
        assert results[0].action == "task.claimed"


class TestBatchAuditLoggerQuery:
    def test_task_id_filter_in_memory_path(self):
        cfg = AuditConfig(enabled=False)  # don't start background flush task
        bal = BatchAuditLogger(config=cfg)
        bal._in_memory.extend([
            _entry("task.claimed", task_id="t-1"),
            _entry("task.handoff", task_id="t-1"),
            _entry("task.claimed", task_id="t-2"),
        ])
        results = bal.query(task_id="t-1")
        assert len(results) == 2

    def test_legacy_resource_type_fallback(self):
        cfg = AuditConfig(enabled=False)
        bal = BatchAuditLogger(config=cfg)
        legacy = AuditLogEntry(
            action="task.rejected",
            resource_type="task",
            resource_id="t-1",
        )
        bal._in_memory.append(legacy)
        results = bal.query(task_id="t-1")
        assert len(results) == 1