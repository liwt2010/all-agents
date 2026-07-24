"""
Tests for AccessControl wiring in task routes (v0.6.0-4).

Covers:
  - Cross-tenant 404 (existing behavior preserved)
  - PRIVATE visibility: owner can read, others 403
  - TENANT_PUBLIC: any same-tenant user can read
  - shared_with: specific user can read
  - platform_admin: sees all
  - tenant_admin: sees all in own tenant
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_system.storage.task_store import InMemoryTaskStore, TaskRecord


def _record(
    task_id: str = "t-1",
    tenant_id: str = "acme",
    owner_id: str = "alice",
    visibility: str = "private",
    **kwargs,
) -> TaskRecord:
    return TaskRecord(
        id=task_id,
        agent="product",
        input="hi",
        status="completed",
        tenant_id=tenant_id,
        user_id=owner_id,
        owner_id=owner_id,
        assignee_id=None,
        version=1,
        visibility=visibility,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        **kwargs,
    )


def _user(
    user_id: str = "alice",
    tenant_id: str = "acme",
    role: str = "user",
) -> object:
    """Lightweight User-like object compatible with _to_user_ctx()."""
    from types import SimpleNamespace
    return SimpleNamespace(
        id=user_id,
        tenant_id=tenant_id,
        global_role=SimpleNamespace(value=role),
        perm_group_ids=[],
        group_ids=[],
        project_ids=[],
        is_agent=False,
    )


# We import the helpers directly so we don't need a live FastAPI server.
from agent_system.api.routes.tasks import (
    _acl,
    _ensure_can_read,
    _record_to_resource,
    _to_user_ctx,
)


class TestVisibilityPrivate:
    def test_owner_can_read_own_private(self):
        rec = _record(owner_id="alice", visibility="private")
        _ensure_can_read(_user("alice", "acme"), rec)  # no exception

    def test_other_user_cannot_read_private(self):
        rec = _record(owner_id="alice", visibility="private")
        with pytest.raises(Exception) as exc:
            _ensure_can_read(_user("bob", "acme"), rec)
        # FastAPI HTTPException — detail must be Access denied.
        assert "Access denied" in str(exc.value)

    def test_cross_tenant_cannot_read(self):
        rec = _record(owner_id="alice", tenant_id="acme", visibility="tenant_public")
        with pytest.raises(Exception) as exc:
            _ensure_can_read(_user("bob", "evilcorp"), rec)
        assert "Access denied" in str(exc.value)


class TestVisibilityTenantPublic:
    def test_any_same_tenant_can_read(self):
        rec = _record(owner_id="alice", tenant_id="acme", visibility="tenant_public")
        _ensure_can_read(_user("bob", "acme"), rec)


class TestVisibilitySharedWith:
    def test_shared_user_can_read(self):
        rec = _record(
            owner_id="alice",
            tenant_id="acme",
            visibility="private",
            metadata={"_shared_with": ["bob"]},
        )
        _ensure_can_read(_user("bob", "acme"), rec)

    def test_unshared_user_still_blocked(self):
        rec = _record(
            owner_id="alice",
            tenant_id="acme",
            visibility="private",
            metadata={"_shared_with": ["carol"]},
        )
        with pytest.raises(Exception):
            _ensure_can_read(_user("bob", "acme"), rec)


class TestVisibilityPlatformAdmin:
    def test_platform_admin_sees_across_tenants(self):
        """platform_admin is the only cross-tenant escape hatch.
        Useful for support / debugging."""
        rec = _record(owner_id="alice", tenant_id="acme", visibility="private")
        _ensure_can_read(_user("root", "evilcorp", role="platform_admin"), rec)

    def test_tenant_admin_blocked_cross_tenant(self):
        rec = _record(owner_id="alice", tenant_id="acme", visibility="private")
        with pytest.raises(Exception):
            _ensure_can_read(_user("root", "evilcorp", role="tenant_admin"), rec)

    def test_tenant_admin_sees_own_tenant(self):
        rec = _record(owner_id="alice", tenant_id="acme", visibility="private")
        _ensure_can_read(_user("admin", "acme", role="tenant_admin"), rec)


class TestResourceMapping:
    def test_to_user_ctx_extracts_role_value(self):
        u = _user(role="platform_admin")
        ctx = _to_user_ctx(u)
        assert ctx.global_role == "platform_admin"
        assert ctx.user_id == u.id
        assert ctx.tenant_id == u.tenant_id

    def test_record_to_resource_picks_up_metadata_acl_flags(self):
        rec = _record(
            owner_id="alice",
            visibility="group",
            metadata={
                "_perm_group_ids": ["pg-1"],
                "_group_ids": ["g-1"],
                "_project_ids": ["p-1"],
                "_shared_with": ["bob"],
                "anything_else": "kept",
            },
        )
        res = _record_to_resource(rec)
        assert res.perm_group_ids == ["pg-1"]
        assert res.group_ids == ["g-1"]
        assert res.project_ids == ["p-1"]
        assert res.shared_with == ["bob"]
        # Non-ACL metadata preserved.
        assert res.metadata.get("anything_else") == "kept"
        # The ACL-prefixed keys must NOT leak into metadata.
        assert "_perm_group_ids" not in res.metadata

    def test_owner_id_falls_back_to_user_id(self):
        """Legacy records may not have owner_id set; we backfill from user_id."""
        # Construct a record manually so we can pass an empty owner_id
        # (the _record helper enforces them to match).
        rec = TaskRecord(
            id="t-legacy",
            agent="product",
            input="hi",
            status="completed",
            tenant_id="acme",
            user_id="legacy_alice",
            owner_id="",  # empty — pre-v0.6.0 record
            assignee_id=None,
            version=1,
            visibility="private",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        res = _record_to_resource(rec)
        assert res.owner_id == "legacy_alice"


class TestListFilter:
    """Filter pattern used by list_tasks; runs ACL.can_read on each."""

    def test_only_visible_returned_in_list(self):
        recs = [
            _record("t-1", owner_id="alice", visibility="private"),
            _record("t-2", owner_id="alice", visibility="tenant_public"),
            _record("t-3", owner_id="bob", visibility="private"),
        ]
        ctx = _to_user_ctx(_user("alice"))
        visible = [r for r in recs if _acl.can_read(ctx, _record_to_resource(r))]
        ids = {r.id for r in visible}
        # Alice sees her own private t-1 + tenant_public t-2; not bob's private t-3.
        assert ids == {"t-1", "t-2"}