"""
Per-backend snapshot sources for backup (PR-13).

Each source produces a directory of files representing a point-in-time snapshot
of one logical component (graph, audit, etc). The actual tar.gz assembly is
done in scheduler.py.
"""

import json
import logging
import shutil
import tarfile
from pathlib import Path
from typing import Optional

from agent_system.core.backup.manifest import ComponentInfo, sha256_file

logger = logging.getLogger(__name__)


def snapshot_graph_json(source_dir: str, dest_dir: Path) -> ComponentInfo:
    """Copy the JSON graph directory tree."""
    src = Path(source_dir)
    if not src.exists():
        return ComponentInfo(name="graph", included=False, extra={"reason": "source_dir not found"})
    graph_dest = dest_dir / "graph"
    graph_dest.mkdir(parents=True, exist_ok=True)
    file_count = 0
    total_bytes = 0
    for f in src.rglob("*"):
        if f.is_file():
            rel = f.relative_to(src)
            dest_file = graph_dest / rel
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest_file)
            file_count += 1
            total_bytes += f.stat().st_size
    # Compute checksum of the whole directory
    return ComponentInfo(
        name="graph",
        file_count=file_count,
        total_bytes=total_bytes,
        sha256=_dir_checksum(graph_dest),
        extra={"source_dir": str(src), "format": "json"},
    )


def snapshot_graph_sqlite(db_path: str, dest_dir: Path) -> ComponentInfo:
    """Snapshot SQLite DB via VACUUM INTO (atomic point-in-time)."""
    src = Path(db_path)
    if not src.exists():
        return ComponentInfo(name="graph", included=False, extra={"reason": "db file not found"})
    graph_dest = dest_dir / "graph"
    graph_dest.mkdir(parents=True, exist_ok=True)
    dest_db = graph_dest / "graph.db"
    try:
        import sqlite3
        conn = sqlite3.connect(str(src))
        try:
            conn.execute(f"VACUUM INTO '{str(dest_db).replace(chr(39), chr(39)*2)}'")
        finally:
            conn.close()
    except Exception as e:
        # Fallback: file copy (less safe, but works)
        logger.warning(f"VACUUM INTO failed ({e}), falling back to file copy")
        shutil.copy2(src, dest_db)
    size = dest_db.stat().st_size
    return ComponentInfo(
        name="graph",
        file_count=1,
        total_bytes=size,
        sha256=sha256_file(dest_db),
        extra={"source_path": str(src), "format": "sqlite", "method": "vacuum_or_copy"},
    )


def snapshot_graph_postgres(conn_params: dict, dest_dir: Path) -> ComponentInfo:
    """Snapshot Postgres via pg_dump.

    conn_params must contain: host, port, database, user, password
    Falls back to a placeholder if pg_dump is unavailable.
    """
    graph_dest = dest_dir / "graph"
    graph_dest.mkdir(parents=True, exist_ok=True)
    dump_path = graph_dest / "graph.dump"
    try:
        import subprocess
        env = {"PGPASSWORD": conn_params.get("password", "")}
        cmd = [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "-h", conn_params.get("host", "localhost"),
            "-p", str(conn_params.get("port", 5432)),
            "-U", conn_params.get("user", "all_agents"),
            "-d", conn_params.get("database", "all_agents"),
            "-f", str(dump_path),
        ]
        subprocess.run(cmd, env=env, check=True, timeout=600)
        size = dump_path.stat().st_size
        return ComponentInfo(
            name="graph",
            file_count=1,
            total_bytes=size,
            sha256=sha256_file(dump_path),
            extra={
                "host": conn_params.get("host"),
                "database": conn_params.get("database"),
                "format": "postgres",
                "method": "pg_dump",
            },
        )
    except Exception as e:
        logger.warning(f"pg_dump failed: {e}; writing placeholder")
        # Write a placeholder so backup can still complete (with a warning)
        dump_path.write_text(
            json.dumps({"warning": "pg_dump not available", "error": str(e)}, indent=2),
            encoding="utf-8",
        )
        return ComponentInfo(
            name="graph",
            file_count=1,
            total_bytes=dump_path.stat().st_size,
            sha256=sha256_file(dump_path),
            extra={"format": "postgres", "method": "fallback", "warning": str(e)},
        )


def snapshot_audit_logs(source_dir: str, dest_dir: Path, retention_days: int = 7) -> ComponentInfo:
    """Copy audit-*.jsonl files newer than retention_days."""
    from datetime import datetime, timezone, timedelta
    src = Path(source_dir)
    if not src.exists():
        return ComponentInfo(name="audit", included=False, extra={"reason": "audit dir not found"})
    audit_dest = dest_dir / "audit"
    audit_dest.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    file_count = 0
    total_bytes = 0
    for f in src.glob("audit-*.jsonl"):
        # Check mtime is recent enough
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            continue
        shutil.copy2(f, audit_dest / f.name)
        file_count += 1
        total_bytes += f.stat().st_size
    return ComponentInfo(
        name="audit",
        file_count=file_count,
        total_bytes=total_bytes,
        sha256=_dir_checksum(audit_dest) if file_count else "",
        extra={"source_dir": str(src), "retention_days": retention_days},
    )


def snapshot_custom_agents(source_dir: str, dest_dir: Path) -> ComponentInfo:
    """Copy custom agent code (.py + .yaml)."""
    src = Path(source_dir)
    if not src.exists():
        return ComponentInfo(name="custom_agents", included=False)
    agents_dest = dest_dir / "custom-agents"
    agents_dest.mkdir(parents=True, exist_ok=True)
    file_count = 0
    total_bytes = 0
    for ext in ("*.py", "*.yaml", "*.yml", "*.json"):
        for f in src.rglob(ext):
            if f.is_file() and "__pycache__" not in f.parts:
                rel = f.relative_to(src)
                dest_file = agents_dest / rel
                dest_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dest_file)
                file_count += 1
                total_bytes += f.stat().st_size
    return ComponentInfo(
        name="custom_agents",
        file_count=file_count,
        total_bytes=total_bytes,
        sha256=_dir_checksum(agents_dest) if file_count else "",
        extra={"source_dir": str(src)},
    )


def snapshot_tasks(source_path: Optional[str], dest_dir: Path) -> ComponentInfo:
    """Snapshot task store file (if it exists as a file; in-memory store returns empty)."""
    tasks_dest = dest_dir / "tasks"
    tasks_dest.mkdir(parents=True, exist_ok=True)
    if not source_path:
        # In-memory store — just write a marker file
        marker = tasks_dest / "store.json"
        marker.write_text(json.dumps({"type": "in_memory", "note": "tasks were in-memory at backup time"}), encoding="utf-8")
        return ComponentInfo(
            name="tasks", file_count=1, total_bytes=marker.stat().st_size,
            sha256=sha256_file(marker),
            extra={"format": "in_memory"},
        )
    src = Path(source_path)
    if not src.exists():
        return ComponentInfo(name="tasks", included=False, extra={"reason": "task store file not found"})
    dest_file = tasks_dest / src.name
    shutil.copy2(src, dest_file)
    return ComponentInfo(
        name="tasks", file_count=1, total_bytes=dest_file.stat().st_size,
        sha256=sha256_file(dest_file),
        extra={"format": "file", "source_path": str(src)},
    )


def _dir_checksum(directory: Path) -> str:
    """Compute a deterministic checksum for a directory tree (sorted file paths + hashes).

    Algorithm (must match _recompute_component_checksum in restore.py):
      For each file in sorted path order:
        update(rel_path as utf-8 bytes)
        update(file contents chunk-by-chunk, 1MB blocks)
    """
    import hashlib
    h = hashlib.sha256()
    if not directory.exists():
        return h.hexdigest()
    for f in sorted(directory.rglob("*")):
        if f.is_file():
            rel = str(f.relative_to(directory)).replace("\\", "/")
            h.update(rel.encode("utf-8"))
            with f.open("rb") as fp:
                while True:
                    chunk = fp.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
    return h.hexdigest()