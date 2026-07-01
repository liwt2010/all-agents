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
