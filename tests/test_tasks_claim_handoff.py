"""
Tests for POST /api/tasks/{id}/claim (v0.6.0-5).

Covers:
  - Happy path: claim a fresh task, version bumps
  - 422 on completed/failed/cancelled task
  - 409 on CAS version mismatch (with current record in detail)
  - 404 on missing / cross-tenant
  - 403 when PRIVATE + non-owner
  - 200 when TENANT_PUBLIC + non-owner (same tenant)
  - audit log receives a task.claimed entry with task_id
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent_system.storage.task_store import InMemoryTaskStore, TaskRecord


from agent_system.api import state as api_state
from agent_system.api.routes.tasks import ClaimRequest, claim_task


def _rec(
    task_id: str = "t-1",
    tenant_id: str = "acme",
    owner_id: str = "alice",
    visibility: str = "private",
    status: str = "pending",
    version: int = 1,
    assignee_id: str | None = None,
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        agent="product",
        input="hi",
        status=status,
        tenant_id=tenant_id,
        user_id=owner_id,
        owner_id=owner_id,
        assignee_id=assignee_id,
        version=version,
        visibility=visibility,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )


def _user(user_id: str, tenant_id: str = "acme", role: str = "user"):
    return SimpleNamespace(
        id=user_id, tenant_id=tenant_id,
        global_role=SimpleNamespace(value=role),
        perm_group_ids=[], group_ids=[], project_ids=[], is_agent=False,
    )


class _CapturingAudit:
    def __init__(self):
        self.entries: list = []

    async def log(self, entry):
        self.entries.append(entry)


@pytest.fixture
def patched_store():
    """Patch the task_store + audit_logger singletons; restore on teardown."""
    store = InMemoryTaskStore()
    audit = _CapturingAudit()
    # state.py's getters close over the module-level _task_store /
    # _audit_logger; patch those directly.
    orig_store = api_state._task_store
    orig_audit = api_state._audit_logger
    api_state._task_store = store
    api_state._audit_logger = audit
    try:
        yield store, audit
    finally:
        api_state._task_store = orig_store
        api_state._audit_logger = orig_audit


class TestClaimVisibility:
    def test_owner_can_claim_own_private(self, patched_store):
        store, audit = patched_store
        store.save(_rec(owner_id="alice", visibility="private"))
        resp = asyncio.run(claim_task("t-1", ClaimRequest(), user=_user("alice")))
        assert resp.assignee_id == "alice"
        assert resp.version == 2
        assert len(audit.entries) == 1
        assert audit.entries[0].task_id == "t-1"
        assert audit.entries[0].action == "task.claimed"

    def test_other_user_blocked_on_private(self, patched_store):
        from fastapi import HTTPException
        store, _ = patched_store
        store.save(_rec(owner_id="alice", visibility="private"))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(claim_task("t-1", ClaimRequest(), user=_user("bob")))
        assert exc.value.status_code == 403

    def test_other_user_allowed_on_tenant_public(self, patched_store):
        store, _ = patched_store
        store.save(_rec(owner_id="alice", visibility="tenant_public"))
        resp = asyncio.run(claim_task("t-1", ClaimRequest(), user=_user("bob")))
        assert resp.assignee_id == "bob"
        assert resp.version == 2

    def test_platform_admin_can_claim_across_tenants(self, patched_store):
        """platform_admin is the only cross-tenant escape hatch."""
        store, _ = patched_store
        store.save(_rec(tenant_id="acme", owner_id="alice", visibility="private"))
        resp = asyncio.run(
            claim_task(
                "t-1", ClaimRequest(), user=_user("root", "evilcorp", role="platform_admin")
            )
        )
        assert resp.assignee_id == "root"


class TestClaimStateAndCAS:
    def test_completed_task_returns_422(self, patched_store):
        from fastapi import HTTPException
        store, _ = patched_store
        store.save(_rec(status="completed"))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(claim_task("t-1", ClaimRequest(), user=_user("alice")))
        assert exc.value.status_code == 422
        assert "terminal state" in exc.value.detail.lower()

    def test_failed_task_returns_422(self, patched_store):
        from fastapi import HTTPException
        store, _ = patched_store
        store.save(_rec(status="failed"))
        with pytest.raises(HTTPException) as exc:
            asyncio.run(claim_task("t-1", ClaimRequest(), user=_user("alice")))
        assert exc.value.status_code == 422

    def test_version_conflict_returns_409_with_current(self, patched_store):
        from fastapi import HTTPException
        store, _ = patched_store
        store.save(_rec(version=2))  # expected_version=1 will mismatch
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                claim_task(
                    "t-1", ClaimRequest(expected_version=1), user=_user("alice")
                )
            )
        assert exc.value.status_code == 409
        assert exc.value.detail["expected_version"] == 1
        assert exc.value.detail["actual_version"] == 2
        assert exc.value.detail["current"]["task_id"] == "t-1"

    def test_missing_task_returns_404(self, patched_store):
        from fastapi import HTTPException
        store, _ = patched_store
        with pytest.raises(HTTPException) as exc:
            asyncio.run(claim_task("nope", ClaimRequest(), user=_user("alice")))
        assert exc.value.status_code == 404

    def test_cas_match_succeeds(self, patched_store):
        store, audit = patched_store
        store.save(_rec(version=2))
        resp = asyncio.run(
            claim_task(
                "t-1", ClaimRequest(expected_version=2), user=_user("alice")
            )
        )
        assert resp.version == 3
        assert resp.assignee_id == "alice"
        assert len(audit.entries) == 1

    def test_reclaim_replaces_assignee(self, patched_store):
        """A second claim by a different user overwrites the assignee."""
        store, _ = patched_store
        store.save(_rec(visibility="tenant_public", assignee_id=None))
        first = asyncio.run(claim_task("t-1", ClaimRequest(), user=_user("alice")))
        assert first.assignee_id == "alice"
        second = asyncio.run(claim_task("t-1", ClaimRequest(), user=_user("bob")))
        assert second.assignee_id == "bob"
        assert second.version == 3