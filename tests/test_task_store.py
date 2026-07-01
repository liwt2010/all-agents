"""
Tests: Postgres backend (storage layer)
"""

import os
import pytest
from datetime import datetime, timezone

from agent_system.storage.task_store import (
    TaskRecord,
    TaskStore,
    InMemoryTaskStore,
    PostgresTaskStore,
    create_task_store,
    get_task_store,
    reset_task_store,
)


# ── In-memory ──

class TestInMemoryTaskStore:
    def test_save_and_get(self):
        store = InMemoryTaskStore()
        record = TaskRecord(
            id="t-1", agent="product", input="build x",
            status="pending", tenant_id="acme",
        )
        store.save(record)
        loaded = store.get("t-1")
        assert loaded is not None
        assert loaded.agent == "product"
        assert loaded.status == "pending"

    def test_update_overwrites(self):
        store = InMemoryTaskStore()
        rec = TaskRecord(id="t-1", agent="product", input="x", status="pending")
        store.save(rec)
        rec.status = "completed"
        rec.completed_at = datetime.now(timezone.utc)
        store.save(rec)
        assert store.get("t-1").status == "completed"

    def test_get_missing(self):
        store = InMemoryTaskStore()
        assert store.get("nonexistent") is None

    def test_list_with_filters(self):
        store = InMemoryTaskStore()
        store.save(TaskRecord(id="t1", agent="a", input="", tenant_id="acme", status="completed"))
        store.save(TaskRecord(id="t2", agent="a", input="", tenant_id="acme", status="failed"))
        store.save(TaskRecord(id="t3", agent="a", input="", tenant_id="beta", status="completed"))

        acme = store.list(tenant_id="acme")
        assert len(acme) == 2
        completed = store.list(tenant_id="acme", status="completed")
        assert len(completed) == 1
        assert completed[0].id == "t1"

    def test_delete(self):
        store = InMemoryTaskStore()
        store.save(TaskRecord(id="t-1", agent="a", input=""))
        assert store.delete("t-1") is True
        assert store.get("t-1") is None
        # Idempotent
        assert store.delete("t-1") is False


# ── Factory ──

class TestFactory:
    def test_force_in_memory(self):
        store = create_task_store(force_in_memory=True)
        assert isinstance(store, InMemoryTaskStore)

    def test_no_url_returns_in_memory(self, monkeypatch):
        monkeypatch.delenv("POSTGRES_URL", raising=False)
        store = create_task_store()
        assert isinstance(store, InMemoryTaskStore)

    def test_bad_url_falls_back_to_in_memory(self):
        """Connection failure should not crash — fall back."""
        # Use an unconnectable URL (bad host)
        store = PostgresTaskStore("postgresql://baduser:badpass@nonexistent:5432/db")
        # Even if the connection failed, the store should be usable (in-memory)
        rec = TaskRecord(id="t-1", agent="a", input="")
        store.save(rec)
        # save falls back to in-memory
        assert store.get("t-1") is not None


# ── Singleton ──

class TestGlobalStore:
    def test_singleton(self):
        s1 = get_task_store()
        s2 = get_task_store()
        assert s1 is s2

    def test_reset(self):
        s1 = get_task_store()
        reset_task_store()
        s2 = get_task_store()
        # After reset, fresh instance
        assert s1 is not s2
