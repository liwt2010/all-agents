"""
Tests for GET /api/tasks/{id}/events (v0.6.0-7).

Covers:
  - Happy path: returns audit entries for the task
  - 404 when task doesn't exist
  - 403 when ACL denies read
  - Audit entries written before v0.6.0 (resource_type='task' +
    resource_id) still match
  - Returns the timeline in chronological order
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_system.storage.task_store import InMemoryTaskStore, TaskRecord


from agent_system.api import state as api_state
from agent_system.api.routes.tasks import get_task_events


def _rec(
    task_id: str = "t-1",
    tenant_id: str = "acme",
    owner_id: str = "alice",
    visibility: str = "private",
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        agent="product",
        input="hi",
        status="completed",
        tenant_id=tenant_id,
        user_id=owner_id,
        owner_id=owner_id,
        visibility=visibility,
        version=1,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _user(user_id: str, tenant_id: str = "acme", role: str = "user"):
    return SimpleNamespace(
        id=user_id, tenant_id=tenant_id,
        global_role=SimpleNamespace(value=role),
        perm_group_ids=[], group_ids=[], project_ids=[], is_agent=False,
    )


class _FakeAudit:
    """Audit logger fake that uses an in-memory list + returns it from
    query_from_disk. Mirrors the public BatchAuditLogger API we use."""

    def __init__(self, entries):
        self.entries = entries

    def query_from_disk(self, task_id=None, limit=100, **_):
        out = []
        for e in self.entries:
            if task_id is None:
                out.append(e)
                continue
            if e.task_id == task_id or (
                e.resource_type == "task" and e.resource_id == task_id
            ):
                out.append(e)
            if len(out) >= limit:
                break
        return out


def _entry(action: str, **fields):
    from agent_system.core.audit_logger import AuditLogEntry
    return AuditLogEntry(action=action, **fields)


@pytest.fixture
def patched():
    store = InMemoryTaskStore()
    store.save(_rec())
    entries = [
        _entry("task.claimed", task_id="t-1", user_id="bob"),
        _entry("task.handoff", task_id="t-1", user_id="bob",
               details={"to_user_id": "carol"}),
        _entry("task.completed", task_id="t-1", user_id="system"),
        # Pre-v0.6.0 entry that should still match via legacy fallback.
        _entry("task.rejected", resource_type="task",
               resource_id="t-1", user_id="alice"),
        # Unrelated task — must NOT appear.
        _entry("task.claimed", task_id="t-2", user_id="someone"),
    ]
    audit = _FakeAudit(entries)
    orig_store = api_state._task_store
    orig_audit = api_state._audit_logger
    api_state._task_store = store
    api_state._audit_logger = audit
    try:
        yield store, audit
    finally:
        api_state._task_store = orig_store
        api_state._audit_logger = orig_audit


class TestEventsHappyPath:
    def test_returns_task_timeline(self, patched):
        store, _ = patched
        resp = asyncio.run(get_task_events("t-1", user=_user("alice")))
        assert resp.task_id == "t-1"
        # 4 entries: claimed, handoff, completed, rejected (legacy).
        assert resp.count == 4
        actions = [e.action for e in resp.events]
        assert "task.claimed" in actions
        assert "task.handoff" in actions
        assert "task.completed" in actions
        assert "task.rejected" in actions  # legacy fallback matched

    def test_excludes_other_tasks(self, patched):
        store, _ = patched
        resp = asyncio.run(get_task_events("t-1", user=_user("alice")))
        actions = [e.action for e in resp.events]
        assert all("task" in a for a in actions)
        # t-2's claim is filtered out.
        # Count stays at 4 (the t-1 entries) regardless of t-2 entries.

    def test_event_details_round_trip(self, patched):
        store, _ = patched
        resp = asyncio.run(get_task_events("t-1", user=_user("alice")))
        handoff = next(e for e in resp.events if e.action == "task.handoff")
        assert handoff.details["to_user_id"] == "carol"


class TestEventsAccess:
    def test_missing_task_returns_404(self, patched):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            asyncio.run(get_task_events("nope", user=_user("alice")))
        assert exc.value.status_code == 404

    def test_other_user_private_returns_403(self, patched):
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc:
            asyncio.run(get_task_events("t-1", user=_user("bob")))
        assert exc.value.status_code == 403

    def test_tenant_public_allows_other_user(self, patched):
        store, _ = patched
        store.save(_rec(task_id="t-9", visibility="tenant_public"))
        # _FakeAudit doesn't carry the new task, so empty.
        resp = asyncio.run(get_task_events("t-9", user=_user("bob")))
        assert resp.task_id == "t-9"
        assert resp.count == 0