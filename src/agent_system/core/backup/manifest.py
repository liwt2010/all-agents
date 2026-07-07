"""
Backup manifest — describes what is in a backup, with checksums (PR-13).

Each backup is a tar.gz containing:
  manifest.json          (this file)
  graph/...              (storage backend dump)
  audit/...              (audit jsonl files)
  custom-agents/...      (custom agent code)
  tasks/...              (task records)

The manifest is loaded first to validate the backup before extraction.
"""

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def sha256_file(path: Path) -> str:
    """Compute SHA-256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ComponentInfo(BaseModel):
    """Info about one backed-up component."""
    name: str
    included: bool = True
    file_count: int = 0
    total_bytes: int = 0
    sha256: str = ""
    extra: Dict[str, Any] = Field(default_factory=dict)


class BackupManifest(BaseModel):
    """Backup manifest — written into every backup tarball."""
    backup_id: str
    created_at: str                    # ISO 8601 UTC
    version: str = "0.1.0"
    backend: str                       # json | sqlite | postgres
    components: Dict[str, ComponentInfo] = Field(default_factory=dict)
    compression: str = "gzip"
    size_bytes: int = 0
    duration_seconds: float = 0.0
    source_host: str = ""
    notes: str = ""

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def from_json(cls, text: str) -> "BackupManifest":
        return cls.model_validate(json.loads(text))

    def to_bytes(self) -> bytes:
        return self.to_json().encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> "BackupManifest":
        return cls.from_json(data.decode("utf-8"))

    def verify_component_checksum(self, component_name: str, actual_sha256: str) -> bool:
        """Compare recorded vs actual checksum. Returns False on mismatch."""
        comp = self.components.get(component_name)
        if comp is None or not comp.included:
            return True  # component not included, vacuously verified
        return comp.sha256 == actual_sha256


def build_backup_id(prefix: str = "backup") -> str:
    """Generate a backup ID like 'backup-20260707-020000'."""
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"