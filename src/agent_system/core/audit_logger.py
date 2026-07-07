"""
Audit logger with async + log rotation support (BLOCKER 5 fix).

Writes audit entries via asyncio.to_thread (non-blocking in event loop)
and includes log rotation via RotatingFileHandler for the Python logger.
"""

import asyncio
import logging
import json
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Redact sensitive data in logs ──

SENSITIVE_LOG_REDACT_PATTERNS = [
    ("api[-_]?key", "***API_KEY***"),
    ("password", "***PASSWORD***"),
    ("secret", "***SECRET***"),
    ("token", "***TOKEN***"),
    ("bearer", "***BEARER***"),
    ("authorization", "***AUTH***"),
    ("sk-[a-zA-Z0-9]+", "***SK***"),              # Anthropic/OpenAI keys
    ("ghp_[a-zA-Z0-9]+", "***GH_TOKEN***"),        # GitHub PAT
    ("gho_[a-zA-Z0-9]+", "***GH_OATH***"),
    ("eyJ[a-zA-Z0-9_\\-.]+", "***JWT***"),         # JWT (starts with eyJ)
    (r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", "***EMAIL***"),
    (r"\b\d{4}[-]?\d{4}[-]?\d{4}[-]?\d{4}\b", "***CC***"),  # credit card
    (r"\b\d{3}[-]?\d{2}[-]?\d{4}\b", "***SSN***"),           # SSN
]

def redact(text: str) -> str:
    """Redact sensitive information from log text."""
    result = text
    for pattern, replacement in SENSITIVE_LOG_REDACT_PATTERNS:
        try:
            import re
            result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
        except Exception:
            pass
    return result[:2000]  # also truncate long lines


# ── Audit entry model ──

class AuditLogEntry(BaseModel):
    """An audit log entry (matches existing schema from security.py)."""
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    user_id: str = ""
    action: str = ""
    resource_id: str = ""
    resource_type: str = ""
    details: Dict[str, Any] = Field(default_factory=dict)
    ip_address: str = ""
    user_agent: str = ""
    outcome: str = "success"  # success / failure / denied

    # PR-11: extended fields for production observability
    request_id: str = ""          # from RequestIDMiddleware (PR-7)
    tenant_id: str = ""           # multi-tenant isolation
    session_id: str = ""          # user session correlation
    duration_ms: float = 0.0      # action execution time


# ── Log rotation setup ──

def configure_logger(
    name: str = "agent_system",
    log_dir: str = "data/logs",
    max_bytes: int = 10 * 1024 * 1024,      # 10 MB per file
    backup_count: int = 10,                  # keep 10 rotated files
    level: int = logging.INFO,
) -> logging.Logger:
    """
    Configure a structured logger with rotation and JSON formatting.
    """
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    log_path = Path(log_dir) / f"{name}.log"

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # File handler with rotation
    file_handler = RotatingFileHandler(
        str(log_path),
        maxBytes=max_bytes,
        backupCount=backup_count,
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    ))
    logger.addHandler(file_handler)

    # Remove default handlers to avoid duplicate output
    logger.propagate = False
    return logger


# Default logger instance
_logger = configure_logger()


def get_logger() -> logging.Logger:
    return _logger


def log_with_redact(logger: logging.Logger, level: int, msg: str, *args, **kwargs):
    """Log with automatic redaction and truncation."""
    safe_msg = redact(msg)
    logger.log(level, safe_msg, *args, **kwargs)


# ── Async audit logger ──

class AuditLogger:
    """
    Async-safe audit log writer.

    - write via asyncio.to_thread (doesn't block event loop)
    - RotatingFileHandler for the underlying log
    - Supports in-memory query for testing
    """

    def __init__(self, log_dir: str = "data/audit"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._in_memory: List[AuditLogEntry] = []

    async def log(self, entry: AuditLogEntry) -> bool:
        """Write an audit entry. Non-blocking."""
        self._in_memory.append(entry)
        date_str = entry.timestamp.strftime("%Y-%m-%d")
        log_file = self.log_dir / f"audit-{date_str}.jsonl"
        line = entry.model_dump_json() + "\n"
        try:
            await asyncio.to_thread(self._write_line, log_file, line)
            return True
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Audit write failed: {e}")
            return False

    def _write_line(self, path: Path, line: str):
        """Synchronous file write (called via asyncio.to_thread)."""
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    def query(
        self,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditLogEntry]:
        """Query audit log from in-memory store."""
        results = []
        for entry in self._in_memory:
            if user_id and entry.user_id != user_id:
                continue
            if action and entry.action != action:
                continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results

    def sync_log(self, entry: AuditLogEntry) -> bool:
        """Synchronous fallback (no event loop available)."""
        import asyncio
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self._sync_write(entry)
        return False

    def _sync_write(self, entry: AuditLogEntry) -> bool:
        self._in_memory.append(entry)
        date_str = entry.timestamp.strftime("%Y-%m-%d")
        log_file = self.log_dir / f"audit-{date_str}.jsonl"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(entry.model_dump_json() + "\n")
            return True
        except Exception:
            return False


# Global default
_default_logger: Optional[AuditLogger] = None


def get_audit_logger() -> AuditLogger:
    global _default_logger
    if _default_logger is None:
        _default_logger = AuditLogger()
    return _default_logger
# ─────────────────────────────────────────────────────────────────────────────
# PR-11: Production audit hardening — batch queue, retention, disk query, config
# ─────────────────────────────────────────────────────────────────────────────


class AuditConfig(BaseModel):
    """Configuration for BatchAuditLogger."""
    enabled: bool = True
    sampling_rate: float = 1.0              # 1.0 = log everything; 0.1 = log 10%
    batch_size: int = 100                   # flush after this many entries
    flush_interval_seconds: float = 5.0     # OR this much time elapsed
    retention_days: int = 90                # purge files older than this
    log_dir: str = "./data/audit"
    queue_max_size: int = 10000             # drop oldest if exceeded


class BatchAuditLogger:
    """
    Production audit logger with batched async writes.

    Drop-in replacement for AuditLogger. Buffers entries in an asyncio.Queue
    and flushes to disk either when batch is full or after flush_interval.
    Falls back to synchronous write if no event loop is running.
    """

    def __init__(self, config: Optional[AuditConfig] = None):
        self.config = config or AuditConfig()
        self._in_memory: List[AuditLogEntry] = []
        self._queue: Optional[asyncio.Queue] = None
        self._flush_task: Optional[asyncio.Task] = None
        self._closed = False
        if self.config.enabled:
            Path(self.config.log_dir).mkdir(parents=True, exist_ok=True)

    def _ensure_queue(self) -> bool:
        """Lazily create the queue (must be inside running event loop)."""
        if self._queue is None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return False  # no event loop; caller falls back to sync
            self._queue = asyncio.Queue(maxsize=self.config.queue_max_size)
            self._flush_task = asyncio.create_task(self._periodic_flush())
        return True

    async def log(self, entry: AuditLogEntry) -> bool:
        """Enqueue an audit entry. Returns False if disabled / dropped."""
        if not self.config.enabled:
            return False
        # Sampling
        if self.config.sampling_rate < 1.0:
            import random
            if random.random() > self.config.sampling_rate:
                return False
        # Always keep in-memory for tests
        self._in_memory.append(entry)
        # Try async path
        if not self._ensure_queue():
            return self._sync_write(entry)
        try:
            self._queue.put_nowait(entry)
            return True
        except asyncio.QueueFull:
            # Backpressure: drop oldest from queue to make room
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                self._queue.put_nowait(entry)
                return True
            except asyncio.QueueFull:
                return False

    def sync_log(self, entry: AuditLogEntry) -> bool:
        """Synchronous entry point (for tests / non-async callers)."""
        if not self.config.enabled:
            return False
        self._in_memory.append(entry)
        return self._sync_write(entry)

    def _sync_write(self, entry: AuditLogEntry) -> bool:
        date_str = entry.timestamp.strftime("%Y-%m-%d")
        log_file = Path(self.config.log_dir) / f"audit-{date_str}.jsonl"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(entry.model_dump_json() + "\n")
            return True
        except Exception:
            return False

    async def _periodic_flush(self) -> None:
        """Background task: flush every flush_interval_seconds."""
        import time as _time
        while not self._closed:
            await asyncio.sleep(self.config.flush_interval_seconds)
            await self._flush_batch()

    async def _flush_batch(self) -> int:
        """Drain up to batch_size entries from queue and write them."""
        if self._queue is None:
            return 0
        batch: List[AuditLogEntry] = []
        for _ in range(self.config.batch_size):
            try:
                entry = self._queue.get_nowait()
                batch.append(entry)
            except asyncio.QueueEmpty:
                break
        if not batch:
            return 0
        return await self._write_batch(batch)

    async def _write_batch(self, batch: List[AuditLogEntry]) -> int:
        """Write a batch of entries as a single file append."""
        # Group by date
        by_date: Dict[str, List[str]] = {}
        for entry in batch:
            date_str = entry.timestamp.strftime("%Y-%m-%d")
            by_date.setdefault(date_str, []).append(entry.model_dump_json())

        def _do_write():
            written = 0
            for date_str, lines in by_date.items():
                log_file = Path(self.config.log_dir) / f"audit-{date_str}.jsonl"
                log_file.parent.mkdir(parents=True, exist_ok=True)
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write("\n".join(lines) + "\n")
                written += len(lines)
            return written

        try:
            return await asyncio.to_thread(_do_write)
        except Exception as e:
            import logging as _logging
            _logging.getLogger(__name__).warning(f"Batch audit write failed: {e}")
            return 0

    async def flush(self) -> int:
        """Force-flush any pending entries. Call before shutdown."""
        return await self._flush_batch()

    async def close(self) -> None:
        """Stop background task, flush remaining entries."""
        self._closed = True
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
        await self.flush()

    # ── Retention ──

    def purge_old_entries(self, retention_days: Optional[int] = None) -> int:
        """Delete audit-*.jsonl files older than retention_days. Returns count deleted."""
        from datetime import datetime, timezone, timedelta
        retention = retention_days if retention_days is not None else self.config.retention_days
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
        deleted = 0
        log_dir = Path(self.config.log_dir)
        if not log_dir.exists():
            return 0
        for f in log_dir.glob("audit-*.jsonl"):
            try:
                # Parse date from filename
                date_str = f.stem.replace("audit-", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if file_date < cutoff:
                    f.unlink()
                    deleted += 1
            except Exception:
                continue
        return deleted

    # ── Disk query ──

    def query_from_disk(
        self,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        outcome: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        request_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditLogEntry]:
        """
        Query audit log entries from disk (JSONL files).

        Filters:
            user_id, action, outcome, request_id: exact match
            start_date, end_date: ISO date strings (YYYY-MM-DD), inclusive

        Limit applied AFTER filtering. Returns up to `limit` entries
        in chronological order (oldest first).
        """
        from datetime import datetime
        log_dir = Path(self.config.log_dir)
        if not log_dir.exists():
            return []
        # Determine date range from filenames
        start = datetime.fromisoformat(start_date) if start_date else None
        end = datetime.fromisoformat(end_date) if end_date else None
        results: List[AuditLogEntry] = []
        for jsonl_file in sorted(log_dir.glob("audit-*.jsonl")):
            # Quick skip if filename date out of range
            try:
                date_str = jsonl_file.stem.replace("audit-", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if start and file_date < start:
                    continue
                if end and file_date > end:
                    continue
            except Exception:
                continue
            # Read file
            try:
                with jsonl_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            entry = AuditLogEntry(**data)
                        except Exception:
                            continue
                        if user_id and entry.user_id != user_id:
                            continue
                        if action and entry.action != action:
                            continue
                        if outcome and entry.outcome != outcome:
                            continue
                        if request_id and entry.request_id != request_id:
                            continue
                        results.append(entry)
                        if len(results) >= limit:
                            return results
            except Exception:
                continue
        return results

    # ── Legacy query API (kept for backwards compat) ──

    def query(
        self,
        user_id: Optional[str] = None,
        action: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        limit: int = 100,
    ) -> List[AuditLogEntry]:
        """Query from in-memory store (legacy API)."""
        results = []
        for entry in self._in_memory:
            if user_id and entry.user_id != user_id:
                continue
            if action and entry.action != action:
                continue
            results.append(entry)
            if len(results) >= limit:
                break
        return results


# Override the default logger to be BatchAuditLogger
_default_logger: Optional[BatchAuditLogger] = None


def get_audit_logger() -> BatchAuditLogger:
    """Get the global BatchAuditLogger (singleton)."""
    global _default_logger
    if _default_logger is None:
        # Read config from env if set
        config = AuditConfig(
            enabled=os.environ.get("AGENT_AUDIT_ENABLED", "true").lower() in ("1", "true", "yes"),
            sampling_rate=float(os.environ.get("AGENT_AUDIT_SAMPLING_RATE", "1.0")),
            batch_size=int(os.environ.get("AGENT_AUDIT_BATCH_SIZE", "100")),
            flush_interval_seconds=float(os.environ.get("AGENT_AUDIT_FLUSH_INTERVAL", "5.0")),
            retention_days=int(os.environ.get("AGENT_AUDIT_RETENTION_DAYS", "90")),
            log_dir=os.environ.get("AGENT_AUDIT_LOG_DIR", "./data/audit"),
        )
        _default_logger = BatchAuditLogger(config)
    return _default_logger


def reset_audit_logger() -> None:
    """Drop the singleton (used in tests)."""
    global _default_logger
    _default_logger = None