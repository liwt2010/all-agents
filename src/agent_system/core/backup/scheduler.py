"""
Backup scheduler — orchestrates periodic backups (PR-13).

Reads a cron-like schedule string and runs backups in the background.
Each backup produces a tar.gz + manifest.json in backup_dir.
Retention policy auto-deletes old backups.
"""

import asyncio
import json
import logging
import os
import socket
import tarfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from agent_system.core.backup.manifest import (
    BackupManifest,
    ComponentInfo,
    build_backup_id,
)
from agent_system.core.backup.sources import (
    snapshot_audit_logs,
    snapshot_custom_agents,
    snapshot_graph_json,
    snapshot_graph_postgres,
    snapshot_graph_sqlite,
    snapshot_tasks,
)

logger = logging.getLogger(__name__)


class BackupConfig(BaseModel):
    enabled: bool = True
    schedule_cron: str = "0 2 * * *"            # 02:00 daily
    backup_dir: str = "./data/backup"
    retention_days: int = 7
    include_graph: bool = True
    include_audit: bool = True
    include_custom_agents: bool = True
    include_tasks: bool = True
    audit_retention_days: int = 7
    compression: str = "gzip"
    # Source paths
    graph_json_dir: str = "./data/graph"
    graph_sqlite_path: Optional[str] = None
    graph_postgres_params: Optional[dict] = None
    audit_log_dir: str = "./data/audit"
    custom_agents_dir: str = "./data/custom_agents"
    task_store_path: Optional[str] = None


def load_backup_config_from_env() -> BackupConfig:
    """Read backup config from env vars with defaults."""
    return BackupConfig(
        enabled=os.environ.get("AGENT_BACKUP_ENABLED", "true").lower() in ("1", "true", "yes"),
        schedule_cron=os.environ.get("AGENT_BACKUP_SCHEDULE_CRON", "0 2 * * *"),
        backup_dir=os.environ.get("AGENT_BACKUP_DIR", "./data/backup"),
        retention_days=int(os.environ.get("AGENT_BACKUP_RETENTION_DAYS", "7")),
        graph_json_dir=os.environ.get("AGENT_JSON_DIR", "./data/graph"),
        graph_sqlite_path=os.environ.get("AGENT_SQLITE_PATH"),
        audit_log_dir=os.environ.get("AGENT_AUDIT_LOG_DIR", "./data/audit"),
        custom_agents_dir=os.environ.get("AGENT_CUSTOM_AGENTS_DIR", "./data/custom_agents"),
    )


def create_backup(config: BackupConfig, storage_backend: str = "json") -> BackupManifest:
    """
    Create a single backup synchronously and return its manifest.

    Steps:
      1. Build a temp working dir
      2. Snapshot each enabled component
      3. Write manifest.json
      4. Tar everything into backup_dir/<backup_id>.tar.gz
      5. Apply retention
    """
    started = time.monotonic()
    backup_id = build_backup_id()
    backup_dir = Path(config.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    work_dir = backup_dir / f".work-{backup_id}"
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        components: dict = {}
        # ── Graph ──
        if config.include_graph:
            graph_dest = work_dir / "components"
            graph_dest.mkdir(exist_ok=True)
            if storage_backend == "json":
                comp = snapshot_graph_json(config.graph_json_dir, graph_dest)
            elif storage_backend == "sqlite" and config.graph_sqlite_path:
                comp = snapshot_graph_sqlite(config.graph_sqlite_path, graph_dest)
            elif storage_backend == "postgres" and config.graph_postgres_params:
                comp = snapshot_graph_postgres(config.graph_postgres_params, graph_dest)
            else:
                comp = ComponentInfo(name="graph", included=False, extra={"reason": "no source configured"})
            components["graph"] = comp

        # ── Audit ──
        if config.include_audit:
            comp = snapshot_audit_logs(
                config.audit_log_dir, work_dir / "components",
                retention_days=config.audit_retention_days,
            )
            components["audit"] = comp

        # ── Custom agents ──
        if config.include_custom_agents:
            comp = snapshot_custom_agents(config.custom_agents_dir, work_dir / "components")
            components["custom_agents"] = comp

        # ── Tasks ──
        if config.include_tasks:
            comp = snapshot_tasks(config.task_store_path, work_dir / "components")
            components["tasks"] = comp

        manifest = BackupManifest(
            backup_id=backup_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            backend=storage_backend,
            components=components,
            compression=config.compression,
            source_host=socket.gethostname(),
            duration_seconds=round(time.monotonic() - started, 3),
        )

        # Write manifest into work dir
        manifest_path = work_dir / "manifest.json"
        manifest_path.write_text(manifest.to_json(), encoding="utf-8")

        # Tar everything up
        tar_path = backup_dir / f"{backup_id}.tar.gz"
        with tarfile.open(str(tar_path), "w:gz") as tar:
            tar.add(str(manifest_path), arcname="manifest.json")
            comps_dir = work_dir / "components"
            if comps_dir.exists():
                tar.add(str(comps_dir), arcname="components")

        # Update manifest with final size
        manifest.size_bytes = tar_path.stat().st_size
        manifest.duration_seconds = round(time.monotonic() - started, 3)
        # Rewrite manifest INSIDE the tar (rewrite the whole archive)
        _rewrite_manifest_in_tar(tar_path, manifest)

        # Apply retention
        deleted = apply_retention(backup_dir, config.retention_days)

        logger.info(
            f"Backup {backup_id} complete in {manifest.duration_seconds}s, "
            f"size={manifest.size_bytes} bytes, retention_purged={deleted}"
        )
        return manifest
    finally:
        # Cleanup work dir
        if work_dir.exists():
            shutil_rmtree(work_dir)


def apply_retention(backup_dir: Path, retention_days: int) -> int:
    """Delete backup-*.tar.gz files older than retention_days. Returns count deleted."""
    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = 0
    if not backup_dir.exists():
        return 0
    for tar in backup_dir.glob("backup-*.tar.gz"):
        try:
            mtime = datetime.fromtimestamp(tar.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                tar.unlink()
                deleted += 1
        except Exception as e:
            logger.warning(f"Failed to apply retention to {tar}: {e}")
    return deleted


def _rewrite_manifest_in_tar(tar_path: Path, manifest: BackupManifest) -> None:
    """Rewrite the manifest.json inside a tar.gz with updated size/duration."""
    import tarfile
    import io
    tmp_path = tar_path.with_suffix(".tar.gz.tmp")
    with tarfile.open(str(tar_path), "r:gz") as src, \
         tarfile.open(str(tmp_path), "w:gz") as dst:
        for member in src:
            if member.name == "manifest.json":
                new_data = manifest.to_bytes()
                info = tarfile.TarInfo(name="manifest.json")
                info.size = len(new_data)
                info.mtime = time.time()
                dst.addfile(info, io.BytesIO(new_data))
            else:
                extracted = src.extractfile(member)
                if extracted is not None:
                    dst.addfile(member, extracted)
                else:
                    dst.addfile(member)
    tmp_path.replace(tar_path)


def shutil_rmtree(path: Path) -> None:
    """Robust rmtree that doesn't fail on read-only files (Windows)."""
    import shutil
    import stat
    def onerror(func, path, _exc_info):
        try:
            os.chmod(path, stat.S_IWRITE)
            func(path)
        except Exception:
            pass
    shutil.rmtree(str(path), onerror=onerror)


class BackupScheduler:
    """Async background task that runs create_backup on a cron schedule."""

    def __init__(self, config: BackupConfig, storage_backend: str = "json"):
        self.config = config
        self.storage_backend = storage_backend
        self._task: Optional[asyncio.Task] = None
        self._stopped = False

    async def start(self) -> None:
        if not self.config.enabled:
            logger.info("BackupScheduler disabled by config")
            return
        self._stopped = False
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"BackupScheduler started (schedule: {self.config.schedule_cron})")

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_loop(self) -> None:
        """Sleep until next scheduled time, then run backup."""
        while not self._stopped:
            wait_seconds = _seconds_until_next_cron(self.config.schedule_cron)
            logger.info(f"Next backup in {wait_seconds:.0f}s")
            try:
                await asyncio.sleep(wait_seconds)
            except asyncio.CancelledError:
                break
            if self._stopped:
                break
            try:
                await asyncio.to_thread(create_backup, self.config, self.storage_backend)
            except Exception as e:
                logger.exception(f"Backup failed: {e}")


def _seconds_until_next_cron(cron_str: str) -> float:
    """
    Crude cron parser: supports 'minute hour * * *' syntax.
    For full cron syntax, use a library. We support minute and hour fields only.
    Returns seconds until next match.
    """
    try:
        parts = cron_str.split()
        if len(parts) < 2:
            return 3600.0  # fallback: 1h
        minute = int(parts[0])
        hour = int(parts[1]) if parts[1] != "*" else None
        now = datetime.now()
        target = now.replace(minute=minute, second=0, microsecond=0)
        if hour is not None:
            target = target.replace(hour=hour)
        # If target is in the past, move to next day
        if target <= now:
            from datetime import timedelta
            target = target + timedelta(days=1)
        return max(60.0, (target - now).total_seconds())
    except Exception:
        return 3600.0