"""
Tests for TaskStore CAS primitives (v0.6.0).

Covers:
  - InMemoryTaskStore.update_fields / complete / fail happy + conflict
  - VersionConflict exception carries the current record
  - PostgresTaskStore path is covered indirectly by InMemory (the
    SQL is identical for the CAS semantics; integration test lives
    in tests/test_task_store_integration.py when POSTGRES_URL is set)

Baseline (v0.5.0): 1040 passed. v0.6.0-2 adds 12 tests here.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent_system.storage.task_store import (
    InMemoryTaskStore,
    TaskRecord,
    VersionConflict,
)


@pytest.fixture
def store() -> InMemoryTaskStore:
    s = InMemoryTaskStore()
    s.save(TaskRecord(
        id="t-1",
        agent="product",
        input="hello",
        tenant_id="acme",
        user_id="alice",
        owner_id="alice",
        version=1,
        visibility="private",
        created_at=datetime.now(timezone.utc),
    ))
    return s


class TestUpdateFieldsHappyPath:
    def test_assignee_id_change_bumps_version(self, store):
        rec = store.update_fields("t-1", expected_version=1, assignee_id="bob")
        assert rec.assignee_id == "bob"
        assert rec.version == 2
        assert rec.updated_at is not None

    def test_multiple_field_change_bumps_version_once(self, store):
        rec = store.update_fields(
            "t-1", expected_version=1,
            assignee_id="bob", visibility="tenant_public",
        )
        assert rec.assignee_id == "bob"
        assert rec.visibility == "tenant_public"
        assert rec.version == 2  # only one bump regardless of field count

    def test_chained_updates_keep_bumping(self, store):
        store.update_fields("t-1", 1, assignee_id="bob")
        store.update_fields("t-1", 2, assignee_id="carol")
        rec = store.update_fields("t-1", 3, assignee_id="dave")
        assert rec.assignee_id == "dave"
        assert rec.version == 4


class TestUpdateFieldsConflict:
    def test_wrong_version_raises(self, store):
        with pytest.raises(VersionConflict) as exc_info:
            store.update_fields("t-1", expected_version=99, assignee_id="eve")
        assert exc_info.value.expected == 99
        assert exc_info.value.actual == 1
        assert exc_info.value.current.assignee_id is None  # unchanged
        # Verify the underlying record is unchanged.
        cur = store.get("t-1")
        assert cur.version == 1
        assert cur.assignee_id is None

    def test_zero_version_raises(self, store):
        with pytest.raises(VersionConflict):
            store.update_fields("t-1", expected_version=0, status="running")

    def test_missing_task_raises_keyerror(self, store):
        with pytest.raises(KeyError):
            store.update_fields("nope", expected_version=1, assignee_id="bob")

    def test_bad_field_name_raises_valueerror(self, store):
        with pytest.raises(ValueError, match="cannot update fields"):
            store.update_fields("t-1", 1, secret_field="x")

    def test_owner_id_immutable_via_cas(self, store):
        """Even with CAS, owner_id cannot be reassigned (collaboration
        invariant — owner is always the creator)."""
        with pytest.raises(ValueError, match="cannot update fields"):
            store.update_fields("t-1", 1, owner_id="eve")


class TestCompleteAndFail:
    def test_complete_sets_status_and_completed_at(self, store):
        rec = store.complete("t-1", expected_version=1, output={"text": "ok"})
        assert rec.status == "completed"
        assert rec.output == {"text": "ok"}
        assert rec.completed_at is not None
        assert rec.version == 2

    def test_complete_cas_conflict(self, store):
        with pytest.raises(VersionConflict):
            store.complete("t-1", expected_version=99, output={"text": "ok"})

    def test_fail_sets_status_and_error(self, store):
        rec = store.fail("t-1", expected_version=1, error="boom")
        assert rec.status == "failed"
        assert rec.error == "boom"
        assert rec.completed_at is not None
        assert rec.version == 2

    def test_fail_cas_conflict(self, store):
        with pytest.raises(VersionConflict):
            store.fail("t-1", expected_version=0, error="boom")