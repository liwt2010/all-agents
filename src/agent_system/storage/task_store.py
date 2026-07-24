"""
Postgres backend (PLATFORM §15) — SQLAlchemy models for tasks, agents,
outputs, checkpoints. Optional: if POSTGRES_URL is not set, falls back
to an in-memory store. The interface is identical either way so the
rest of the system doesn't need to know.

Schema:
  - tasks: per-task state + metadata
  - agents: registered agent configs
  - outputs: agent output payloads (JSONB)
  - checkpoints: resume state
  - audit_log: already covered by file-based AuditLogger
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict

logger = logging.getLogger(__name__)


# ── Pydantic models (the public interface) ──

class TaskRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    agent: str
    input: str
    status: str = "pending"  # pending / running / completed / failed
    tenant_id: str = "default"
    user_id: str = ""
    output: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # v0.6.0 — collaboration primitives
    # owner_id is immutable (always the creator); assignee_id flows
    # through claim / handoff; version is the CAS counter; visibility
    # is one of SpaceVisibility enum values (string form for storage).
    owner_id: str = ""
    assignee_id: str | None = None
    version: int = 1
    visibility: str = "private"
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Backend interface ──

class TaskStore:
    """
    Storage for TaskRecord. Abstract interface — concrete impls are
    InMemoryTaskStore and PostgresTaskStore.
    """

    def save(self, record: TaskRecord) -> None:
        raise NotImplementedError

    def get(self, task_id: str) -> TaskRecord | None:
        raise NotImplementedError

    def list(
        self,
        tenant_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[TaskRecord]:
        raise NotImplementedError

    def delete(self, task_id: str) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ── In-memory implementation (default) ──

class InMemoryTaskStore(TaskStore):
    def __init__(self):
        self._store: dict[str, TaskRecord] = {}

    def save(self, record: TaskRecord) -> None:
        self._store[record.id] = record

    def get(self, task_id: str) -> TaskRecord | None:
        return self._store.get(task_id)

    def list(
        self,
        tenant_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[TaskRecord]:
        out = list(self._store.values())
        if tenant_id:
            out = [r for r in out if r.tenant_id == tenant_id]
        if status:
            out = [r for r in out if r.status == status]
        return out[-limit:]

    def delete(self, task_id: str) -> bool:
        if task_id in self._store:
            del self._store[task_id]
            return True
        return False


# ── Postgres implementation ──

class PostgresTaskStore(TaskStore):
    """
    SQLAlchemy-backed store. Tables are auto-created on first connect.

    Requires `POSTGRES_URL` env var. Falls back to in-memory if not set
    or if connection fails.
    """

    def __init__(self, url: str, fallback: TaskStore | None = None):
        try:
            from sqlalchemy import create_engine, Column, String, DateTime, JSON, Text
            from sqlalchemy.orm import declarative_base, sessionmaker
        except ImportError:
            raise ImportError("PostgresTaskStore requires sqlalchemy. Run: pip install sqlalchemy")

        self._sa = __import__("sqlalchemy", fromlist=["create_engine", "Column", "String", "DateTime", "JSON", "Text", "declarative_base", "sessionmaker"])
        try:
            self._engine = create_engine(url, pool_pre_ping=False, future=True)
            # Test connection early so bad URLs are caught here
            with self._engine.connect() as conn:
                conn.execute(self._sa.__import__("sqlalchemy").text("SELECT 1"))
        except Exception as e:
            logger.warning(f"Postgres connection failed at init ({e}); using fallback")
            self._engine = None
            self._fallback = fallback or InMemoryTaskStore()
            return
        self._SessionLocal = sessionmaker(bind=self._engine, expire_on_commit=False)

        Base = declarative_base()

        class TaskRow(Base):
            __tablename__ = "tasks"
            id = Column(String, primary_key=True)
            agent = Column(String, nullable=False)
            input = Column(Text)
            status = Column(String, default="pending")
            tenant_id = Column(String, default="default")
            user_id = Column(String, default="")
            output = Column(JSON)
            error = Column(Text)
            started_at = Column(DateTime(timezone=True))
            completed_at = Column(DateTime(timezone=True))
            metadata_ = Column("metadata", JSON, default=dict)
            # v0.6.0 — collaboration primitives
            owner_id = Column(String, nullable=False, default="")
            assignee_id = Column(String)
            version = Column(self._sa.__import__("sqlalchemy").Integer, nullable=False, default=1)
            visibility = Column(String, nullable=False, default="private")
            created_at = Column(DateTime(timezone=True))
            updated_at = Column(DateTime(timezone=True))

        self._TaskRow = TaskRow
        self._Base = Base
        self._fallback = fallback

        try:
            Base.metadata.create_all(self._engine)
            logger.info(f"PostgresTaskStore connected: {url.split('@')[-1] if '@' in url else url}")
        except Exception as e:
            logger.warning(f"Postgres connection failed ({e}); using fallback")
            self._engine = None
            self._fallback = fallback or InMemoryTaskStore()

    def save(self, record: TaskRecord) -> None:
        if self._engine is None:
            return self._fallback.save(record)
        with self._SessionLocal() as session:
            row = session.get(self._TaskRow, record.id)
            if row is None:
                row = self._TaskRow(id=record.id)
                session.add(row)
            row.agent = record.agent
            row.input = record.input
            row.status = record.status
            row.tenant_id = record.tenant_id
            row.user_id = record.user_id
            row.output = record.output
            row.error = record.error
            row.started_at = record.started_at
            row.completed_at = record.completed_at
            row.metadata_ = record.metadata
            row.owner_id = record.owner_id
            row.assignee_id = record.assignee_id
            row.version = record.version
            row.visibility = record.visibility
            row.created_at = record.created_at
            row.updated_at = record.updated_at
            session.commit()

    def get(self, task_id: str) -> TaskRecord | None:
        if self._engine is None:
            return self._fallback.get(task_id)
        with self._SessionLocal() as session:
            row = session.get(self._TaskRow, task_id)
            if row is None:
                return None
            return self._row_to_record(row)

    def list(
        self,
        tenant_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[TaskRecord]:
        if self._engine is None:
            return self._fallback.list(tenant_id=tenant_id, status=status, limit=limit)
        from sqlalchemy import select
        with self._SessionLocal() as session:
            stmt = select(self._TaskRow)
            if tenant_id:
                stmt = stmt.where(self._TaskRow.tenant_id == tenant_id)
            if status:
                stmt = stmt.where(self._TaskRow.status == status)
            stmt = stmt.order_by(self._TaskRow.started_at.desc()).limit(limit)
            rows = session.execute(stmt).scalars().all()
            return [self._row_to_record(r) for r in rows]

    def delete(self, task_id: str) -> bool:
        if self._engine is None:
            return self._fallback.delete(task_id)
        with self._SessionLocal() as session:
            row = session.get(self._TaskRow, task_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

    def close(self) -> None:
        if self._engine is not None:
            self._engine.dispose()

    def _row_to_record(self, row) -> TaskRecord:
        return TaskRecord(
            id=row.id,
            agent=row.agent,
            input=row.input or "",
            status=row.status or "pending",
            tenant_id=row.tenant_id or "default",
            user_id=row.user_id or "",
            output=row.output,
            error=row.error,
            started_at=row.started_at,
            completed_at=row.completed_at,
            metadata=row.metadata_ or {},
            owner_id=row.owner_id or "",
            assignee_id=row.assignee_id,
            version=row.version or 1,
            visibility=row.visibility or "private",
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


# ── Factory ──

def create_task_store(
    postgres_url: str | None = None,
    force_in_memory: bool = False,
) -> TaskStore:
    """
    Factory: returns a PostgresTaskStore if URL is provided, else
    InMemoryTaskStore. The Postgres store itself falls back to
    in-memory if the connection fails at construction time.
    """
    if force_in_memory or not postgres_url:
        return InMemoryTaskStore()
    return PostgresTaskStore(postgres_url)


# Global default
_default_store: TaskStore | None = None


def get_task_store() -> TaskStore:
    global _default_store
    if _default_store is None:
        db_url = os.environ.get("POSTGRES_URL")
        _default_store = create_task_store(db_url)
    return _default_store


def reset_task_store():
    """For tests — clear the global store."""
    global _default_store
    if _default_store is not None:
        _default_store.close()
    _default_store = None
