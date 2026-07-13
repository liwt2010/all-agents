"""
Restore from a backup tarball (PR-13).

Usage:
    python -m agent_system.core.backup.restore \\
        --from ./data/backup/backup-20260707-020000.tar.gz \\
        --target-backend sqlite --target-path ./data/restored.db \\
        --verify

Steps:
    1. Open tar.gz, read manifest.json
    2. Verify component checksums (if --verify)
    3. Extract components to target directory
    4. Print restore report
"""

import argparse
import json
import logging
import os
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Optional

from agent_system.core.backup.manifest import (
    BackupManifest,
    sha256_file,
)

logger = logging.getLogger(__name__)


def restore_from_tar(
    tar_path: str,
    target_dir: str,
    verify: bool = True,
    components_to_restore: list | None = None,
) -> dict:
    """
    Restore from a backup tarball.

    Args:
        tar_path: Path to backup-*.tar.gz
        target_dir: Destination directory
        verify: If True, verify component checksums before extracting
        components_to_restore: List of component names to restore (default: all)

    Returns:
        dict with restore report
    """
    tar_path = Path(tar_path)
    target_dir = Path(target_dir)
    report = {
        "tar_path": str(tar_path),
        "target_dir": str(target_dir),
        "verified": False,
        "manifest": None,
        "components_restored": [],
        "errors": [],
    }

    if not tar_path.exists():
        report["errors"].append(f"tarball not found: {tar_path}")
        return report

    target_dir.mkdir(parents=True, exist_ok=True)

    # ── Open tar and read manifest ──
    try:
        with tarfile.open(str(tar_path), "r:gz") as tar:
            # Find manifest.json
            manifest_member = None
            for member in tar.getmembers():
                if member.name == "manifest.json":
                    manifest_member = member
                    break
            if manifest_member is None:
                report["errors"].append("manifest.json not found in tarball")
                return report
            manifest_bytes = tar.extractfile(manifest_member).read()
            manifest = BackupManifest.from_bytes(manifest_bytes)
            report["manifest"] = manifest.model_dump()

            # ── Verify ──
            if verify:
                # Re-checksum each included component
                for comp_name, comp_info in manifest.components.items():
                    if not comp_info.included:
                        continue
                    comp_dir = None
                    # Find the directory inside tar
                    for m in tar.getmembers():
                        if m.name.startswith(f"components/{comp_name}"):
                            comp_dir = comp_name
                            break
                    if comp_dir is None:
                        # Try alternative: any file under components/<name>
                        for m in tar.getmembers():
                            if m.name.startswith(f"components/{comp_name}/") or m.name == f"components/{comp_name}":
                                comp_dir = comp_name
                                break
                    if comp_dir is None:
                        # Component directory not in tar
                        continue
                    # Re-compute actual checksum
                    actual = _recompute_component_checksum(tar, comp_name)
                    if actual != comp_info.sha256:
                        report["errors"].append(
                            f"component {comp_name} checksum mismatch: "
                            f"expected {comp_info.sha256[:16]}..., got {actual[:16]}..."
                        )
                report["verified"] = len([e for e in report["errors"] if "checksum" in e]) == 0
                if not report["verified"]:
                    return report

            # ── Extract ──
            to_restore = set(components_to_restore) if components_to_restore else None
            for member in tar.getmembers():
                if member.name == "manifest.json":
                    continue
                if not member.name.startswith("components/"):
                    continue
                rel_path = member.name[len("components/"):]
                comp_name = rel_path.split("/", 1)[0]
                if to_restore is not None and comp_name not in to_restore:
                    continue
                # Extract
                target_path = target_dir / rel_path
                if member.isdir():
                    target_path.mkdir(parents=True, exist_ok=True)
                else:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    f = tar.extractfile(member)
                    if f is not None:
                        target_path.write_bytes(f.read())
                if comp_name not in report["components_restored"]:
                    report["components_restored"].append(comp_name)

    except Exception as e:
        report["errors"].append(f"restore failed: {e}")
        logger.exception("Restore failed")

    return report


def _recompute_component_checksum(tar: tarfile.TarFile, component_name: str) -> str:
    """Recompute the directory checksum for a component inside the tar.

    Must match _dir_checksum in sources.py exactly:
      For each file in sorted path order (under components/<name>/):
        update(rel_path as utf-8 bytes)
        update(file contents chunk-by-chunk, 1MB blocks)
    """
    import hashlib
    h = hashlib.sha256()
    members = sorted(
        [m for m in tar.getmembers() if m.name.startswith(f"components/{component_name}/") and m.isfile()],
        key=lambda m: m.name,
    )
    for m in members:
        rel = m.name[len(f"components/{component_name}/"):]
        h.update(rel.replace("\\", "/").encode("utf-8"))
        f = tar.extractfile(m)
        if f is not None:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
    return h.hexdigest()


def main(argv: list | None = None):
    parser = argparse.ArgumentParser(description="Restore from backup tarball")
    parser.add_argument("--from", dest="from_path", required=True, help="Path to backup-*.tar.gz")
    parser.add_argument("--target-dir", dest="target_dir", required=True, help="Destination directory")
    parser.add_argument("--no-verify", dest="verify", action="store_false", default=True)
    parser.add_argument("--components", nargs="*", help="Component names to restore (default: all)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    report = restore_from_tar(
        tar_path=args.from_path,
        target_dir=args.target_dir,
        verify=args.verify,
        components_to_restore=args.components,
    )

    print("\n=== Restore Report ===")
    print(f"From:     {report['tar_path']}")
    print(f"To:       {report['target_dir']}")
    print(f"Verified: {report['verified']}")
    if report.get("manifest"):
        m = report["manifest"]
        print(f"Backup:   {m.get('backup_id')} (created {m.get('created_at')})")
        print(f"Backend:  {m.get('backend')}")
        print(f"Size:     {m.get('size_bytes')} bytes")
    print(f"Restored: {report['components_restored']}")
    if report["errors"]:
        print(f"Errors:   {report['errors']}")
        sys.exit(1)
    sys.exit(0 if report["verified"] else 2)


if __name__ == "__main__":
    main()