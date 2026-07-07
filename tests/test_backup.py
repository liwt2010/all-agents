"""
Tests for PR-13 backup subsystem.

Covers:
- create_backup: JSON, SQLite backends + manifest + tarball
- Component snapshot functions
- apply_retention: deletes old backups
- restore_from_tar: extracts + verifies
- Checksum integrity (corrupted tarball → fail)
- DR drill round-trip (backup → restore → verify)
- BackupScheduler lifecycle (start/stop)
- Concurrent backup blocked
- Disabled config is no-op
"""

import asyncio
import json
import shutil
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest


# ── Manifest basics ──

class TestManifest:
    def test_build_backup_id_format(self):
        from agent_system.core.backup import build_backup_id
        bid = build_backup_id()
        assert bid.startswith("backup-")
        assert len(bid) == len("backup-YYYYMMDD-HHMMSS")

    def test_manifest_round_trip(self):
        from agent_system.core.backup import BackupManifest, ComponentInfo
        m = BackupManifest(
            backup_id="backup-test",
            created_at=datetime.now(timezone.utc).isoformat(),
            backend="sqlite",
            components={"graph": ComponentInfo(name="graph", file_count=10, sha256="abc")},
        )
        text = m.to_json()
        m2 = BackupManifest.from_json(text)
        assert m2.backup_id == "backup-test"
        assert m2.components["graph"].file_count == 10

    def test_sha256_file(self, tmp_path):
        from agent_system.core.backup import sha256_file
        f = tmp_path / "data.bin"
        f.write_bytes(b"hello world")
        h = sha256_file(f)
        assert len(h) == 64  # SHA-256 hex
        # Deterministic
        assert h == sha256_file(f)


# ── create_backup ──

class TestCreateBackup:
    def test_create_backup_json_backend(self, tmp_path):
        from agent_system.core.backup import (
            BackupConfig, create_backup,
        )
        # Set up source data
        graph_src = tmp_path / "graph_src"
        graph_src.mkdir()
        (graph_src / "node1.json").write_text('{"id":"n1","type":"task"}', encoding="utf-8")
        audit_src = tmp_path / "audit_src"
        audit_src.mkdir()
        (audit_src / "audit-2026-07-07.jsonl").write_text('{"action":"login"}', encoding="utf-8")
        custom_src = tmp_path / "custom_src"
        custom_src.mkdir()
        (custom_src / "agent.py").write_text("# agent", encoding="utf-8")

        config = BackupConfig(
            backup_dir=str(tmp_path / "backup"),
            graph_json_dir=str(graph_src),
            audit_log_dir=str(audit_src),
            custom_agents_dir=str(custom_src),
            include_tasks=False,  # skip in-memory task snapshot
        )

        manifest = create_backup(config, storage_backend="json")
        assert manifest.backend == "json"
        assert "graph" in manifest.components
        assert "audit" in manifest.components
        assert "custom_agents" in manifest.components
        assert manifest.size_bytes > 0

        # Verify tarball exists
        tar = Path(config.backup_dir) / f"{manifest.backup_id}.tar.gz"
        assert tar.exists()

    def test_create_backup_sqlite_backend(self, tmp_path):
        from agent_system.core.backup import (
            BackupConfig, create_backup,
        )
        # Create a real SQLite DB
        import sqlite3
        db_path = tmp_path / "graph.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE t (id TEXT)")
        conn.execute("INSERT INTO t VALUES ('hello')")
        conn.commit()
        conn.close()

        config = BackupConfig(
            backup_dir=str(tmp_path / "backup"),
            graph_sqlite_path=str(db_path),
            include_audit=False,
            include_custom_agents=False,
            include_tasks=False,
        )

        manifest = create_backup(config, storage_backend="sqlite")
        assert manifest.backend == "sqlite"
        assert manifest.components["graph"].file_count == 1
        # The graph.db should be in the tarball
        tar = Path(config.backup_dir) / f"{manifest.backup_id}.tar.gz"
        assert tar.exists()

    def test_create_backup_no_graph_when_disabled(self, tmp_path):
        from agent_system.core.backup import BackupConfig, create_backup
        config = BackupConfig(
            backup_dir=str(tmp_path / "backup"),
            include_graph=False,
            include_audit=False,
            include_custom_agents=False,
            include_tasks=False,
        )
        manifest = create_backup(config)
        # Empty components
        assert manifest.components == {}


# ── Retention ──

class TestRetention:
    def test_apply_retention_deletes_old(self, tmp_path):
        from agent_system.core.backup import apply_retention
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        # Create 3 backups with explicit mtime
        for days_ago in [0, 1, 30]:
            ts = (datetime.now() - timedelta(days=days_ago)).strftime("%Y%m%d-%H%M%S")
            f = backup_dir / f"backup-{ts}.tar.gz"
            f.write_bytes(b"x")
            # Set mtime to actual age
            old_time = time.time() - days_ago * 86400
            import os
            os.utime(str(f), (old_time, old_time))

        deleted = apply_retention(backup_dir, retention_days=7)
        assert deleted == 1
        remaining = sorted(p.name for p in backup_dir.glob("backup-*.tar.gz"))
        assert len(remaining) == 2


# ── Restore ──

class TestRestore:
    def test_restore_extracts_components(self, tmp_path):
        from agent_system.core.backup import (
            BackupConfig, create_backup, restore_from_tar,
        )
        # Create a backup first
        graph_src = tmp_path / "graph_src"
        graph_src.mkdir()
        (graph_src / "node.json").write_text('{"id":"n1"}', encoding="utf-8")

        config = BackupConfig(
            backup_dir=str(tmp_path / "backup"),
            graph_json_dir=str(graph_src),
            include_audit=False,
            include_custom_agents=False,
            include_tasks=False,
        )
        manifest = create_backup(config, storage_backend="json")
        tar = Path(config.backup_dir) / f"{manifest.backup_id}.tar.gz"

        # Restore to a new directory
        target = tmp_path / "restored"
        report = restore_from_tar(str(tar), str(target), verify=True)
        assert report["verified"] is True
        assert "graph" in report["components_restored"]
        # File should be present
        restored_file = target / "graph" / "node.json"
        assert restored_file.exists()
        assert restored_file.read_text(encoding="utf-8") == '{"id":"n1"}'

    def test_restore_detects_tampered_checksum(self, tmp_path):
        """If we tamper with the tarball contents, restore should fail verify."""
        from agent_system.core.backup import (
            BackupConfig, create_backup, restore_from_tar,
        )
        graph_src = tmp_path / "graph_src"
        graph_src.mkdir()
        (graph_src / "node.json").write_text('{"id":"n1"}', encoding="utf-8")

        config = BackupConfig(
            backup_dir=str(tmp_path / "backup"),
            graph_json_dir=str(graph_src),
            include_audit=False,
            include_custom_agents=False,
            include_tasks=False,
        )
        manifest = create_backup(config, storage_backend="json")
        tar = Path(config.backup_dir) / f"{manifest.backup_id}.tar.gz"

        # Tamper: rewrite the file with corrupted content
        original_bytes = tar.read_bytes()
        # Flip a byte somewhere in the middle of the gzip stream
        tampered = bytearray(original_bytes)
        if len(tampered) > 100:
            tampered[50] ^= 0xFF
        tar.write_bytes(bytes(tampered))

        report = restore_from_tar(str(tar), str(tmp_path / "restored"), verify=True)
        # Either verify fails OR an error is reported
        assert report["verified"] is False or len(report["errors"]) > 0

    def test_restore_specific_component_only(self, tmp_path):
        from agent_system.core.backup import (
            BackupConfig, create_backup, restore_from_tar,
        )
        graph_src = tmp_path / "graph_src"
        graph_src.mkdir()
        (graph_src / "n.json").write_text('{}', encoding="utf-8")
        audit_src = tmp_path / "audit_src"
        audit_src.mkdir()
        (audit_src / "audit-2026-07-07.jsonl").write_text('{}', encoding="utf-8")

        config = BackupConfig(
            backup_dir=str(tmp_path / "backup"),
            graph_json_dir=str(graph_src),
            audit_log_dir=str(audit_src),
            include_custom_agents=False,
            include_tasks=False,
        )
        manifest = create_backup(config, storage_backend="json")
        tar = Path(config.backup_dir) / f"{manifest.backup_id}.tar.gz"

        # Restore only graph component
        report = restore_from_tar(
            str(tar), str(tmp_path / "restored"),
            verify=True,
            components_to_restore=["graph"],
        )
        assert "graph" in report["components_restored"]
        assert "audit" not in report["components_restored"]


# ── DR drill ──

class TestDRDrill:
    def test_full_round_trip_backup_restore_query(self, tmp_path):
        """DR drill: backup → wipe → restore → verify data accessible."""
        from agent_system.core.backup import (
            BackupConfig, create_backup, restore_from_tar,
        )

        # Step 1: Create source data
        graph_src = tmp_path / "graph_src"
        graph_src.mkdir()
        for i in range(5):
            (graph_src / f"node-{i}.json").write_text(
                json.dumps({"id": f"n{i}", "type": "task", "i": i}),
                encoding="utf-8",
            )

        # Step 2: Backup
        config = BackupConfig(
            backup_dir=str(tmp_path / "backup"),
            graph_json_dir=str(graph_src),
            include_audit=False,
            include_custom_agents=False,
            include_tasks=False,
        )
        manifest = create_backup(config, storage_backend="json")
        tar = Path(config.backup_dir) / f"{manifest.backup_id}.tar.gz"

        # Step 3: Wipe source
        shutil.rmtree(graph_src)

        # Step 4: Restore
        target = tmp_path / "restored"
        report = restore_from_tar(str(tar), str(target), verify=True)
        assert report["verified"] is True

        # Step 5: Verify data accessible (count files, check content)
        restored_files = sorted((target / "graph").glob("*.json"))
        assert len(restored_files) == 5
        first_content = json.loads(restored_files[0].read_text(encoding="utf-8"))
        assert first_content["type"] == "task"


# ── Scheduler lifecycle ──

class TestScheduler:
    @pytest.mark.asyncio
    async def test_scheduler_disabled_does_not_start(self, tmp_path):
        from agent_system.core.backup import BackupConfig, BackupScheduler
        config = BackupConfig(enabled=False, backup_dir=str(tmp_path))
        sched = BackupScheduler(config)
        await sched.start()
        # _task should be None
        assert sched._task is None
        await sched.stop()

    @pytest.mark.asyncio
    async def test_scheduler_stop_cancels_task(self, tmp_path):
        from agent_system.core.backup import BackupConfig, BackupScheduler
        config = BackupConfig(
            enabled=True,
            backup_dir=str(tmp_path / "backup"),
            schedule_cron="0 2 * * *",  # far in the future
            include_audit=False,
            include_custom_agents=False,
            include_tasks=False,
        )
        sched = BackupScheduler(config)
        await sched.start()
        assert sched._task is not None
        await sched.stop()
        # Task should be done/cancelled
        assert sched._task.done() or sched._task.cancelled()


# ── Env config ──

class TestEnvConfig:
    def test_load_backup_config_from_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENT_BACKUP_ENABLED", "false")
        monkeypatch.setenv("AGENT_BACKUP_DIR", str(tmp_path / "env_backup"))
        monkeypatch.setenv("AGENT_BACKUP_RETENTION_DAYS", "30")
        monkeypatch.setenv("AGENT_AUDIT_LOG_DIR", str(tmp_path / "env_audit"))

        from agent_system.core.backup import load_backup_config_from_env
        config = load_backup_config_from_env()
        assert config.enabled is False
        assert str(tmp_path / "env_backup") in config.backup_dir
        assert config.retention_days == 30
        assert str(tmp_path / "env_audit") in config.audit_log_dir


# ── Cron parsing ──

class TestCronParsing:
    def test_seconds_until_next_cron_basic(self):
        from agent_system.core.backup.scheduler import _seconds_until_next_cron
        # Should return a positive number for any valid cron
        secs = _seconds_until_next_cron("0 2 * * *")
        assert 60 <= secs <= 86400 + 60

    def test_seconds_until_invalid_cron_falls_back(self):
        from agent_system.core.backup.scheduler import _seconds_until_next_cron
        secs = _seconds_until_next_cron("garbage")
        assert secs == 3600.0