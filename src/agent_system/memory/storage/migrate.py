"""
Migration CLI: convert between storage backends.

Usage:
    python -m agent_system.memory.storage.migrate \\
        --from json --from-path ./data/graph \\
        --to sqlite --to-path ./data/graph.db

    python -m agent_system.memory.storage.migrate \\
        --from sqlite --from-path ./data/dev.db \\
        --to postgres --to-host prod-db --to-database all_agents

The CLI handles node + link transfer with progress reporting.
Backends must implement the GraphStorage Protocol.
"""

import argparse
import logging
import sys
import time
from typing import Optional

logger = logging.getLogger(__name__)


def migrate(
    source,
    target,
    verify: bool = True,
    batch_size: int = 100,
) -> dict:
    """
    Migrate all data from `source` GraphStorage to `target` GraphStorage.

    Steps:
      1. Ensure both backends initialized
      2. Load source nodes, save to target (with progress)
      3. Load source links, save to target (with progress)
      4. Verify counts match (if verify=True)

    Returns a report dict with counts and timing.
    """
    from agent_system.memory.graph import (
        GraphNode,
        GraphLink,
        LinkType,
        NodeType,
    )

    report = {
        "nodes_migrated": 0,
        "links_migrated": 0,
        "elapsed_seconds": 0.0,
        "verified": False,
        "errors": [],
    }
    started = time.monotonic()

    # Initialize target schema
    try:
        target.init()
    except Exception as e:
        report["errors"].append(f"target.init() failed: {e}")
        return report

    # ── Migrate nodes ──
    try:
        nodes = source.list_nodes()
    except Exception as e:
        report["errors"].append(f"source.list_nodes() failed: {e}")
        return report

    logger.info(f"Migrating {len(nodes)} nodes from {source.backend_name()} → {target.backend_name()}")
    for i, node in enumerate(nodes, 1):
        try:
            target.save_node(node)
            report["nodes_migrated"] += 1
        except Exception as e:
            report["errors"].append(f"node {node.id} save failed: {e}")
            logger.warning(f"node {node.id} save failed: {e}")
        if i % batch_size == 0:
            logger.info(f"  nodes: {i}/{len(nodes)}")

    # ── Migrate links ──
    # We need to walk all links from source. There's no list-all-links method in the
    # Protocol; we walk via nodes. But that's O(N²) worst case. Instead, we add a
    # helper: scan source.list_links for each node direction='out'.
    seen_link_keys = set()
    for node in nodes:
        try:
            out_links = source.list_links(node.id, direction="out")
        except Exception as e:
            report["errors"].append(f"list_links({node.id}) failed: {e}")
            continue
        for link in out_links:
            key = (link.source_id, link.target_id, link.link_type.value)
            if key in seen_link_keys:
                continue
            seen_link_keys.add(key)
            try:
                target.save_link(link)
                report["links_migrated"] += 1
            except Exception as e:
                report["errors"].append(f"link {key} save failed: {e}")

    logger.info(f"Migrated {report['links_migrated']} links")

    # ── Verify ──
    if verify:
        try:
            target_nodes = target.list_nodes()
            report["verified"] = len(target_nodes) == len(nodes)
            if not report["verified"]:
                report["errors"].append(
                    f"verification failed: target has {len(target_nodes)} nodes, source had {len(nodes)}"
                )
        except Exception as e:
            report["errors"].append(f"verification failed: {e}")

    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def _build_backend(args, prefix: str):
    """Build a backend instance from argparse args prefixed with --{prefix}-*"""
    from agent_system.memory.storage.factory import get_storage

    backend = getattr(args, f"{prefix}_backend", None)
    if not backend:
        return None

    kwargs = {}
    for key in ("path", "host", "port", "database", "user", "password", "pool_min", "pool_max", "base_dir"):
        val = getattr(args, f"{prefix}_{key}", None)
        if val is not None:
            kwargs[key] = val
    return get_storage(backend, **kwargs)


def main(argv: Optional[list] = None):
    parser = argparse.ArgumentParser(
        description="Migrate graph data between storage backends."
    )
    # Source
    parser.add_argument("--from", dest="from_backend", choices=["json", "sqlite", "postgres"], required=True)
    parser.add_argument("--from-path", dest="from_path")
    parser.add_argument("--from-host", dest="from_host")
    parser.add_argument("--from-port", dest="from_port", type=int)
    parser.add_argument("--from-database", dest="from_database")
    parser.add_argument("--from-user", dest="from_user")
    parser.add_argument("--from-password", dest="from_password")
    parser.add_argument("--from-base-dir", dest="from_base_dir")
    # Target
    parser.add_argument("--to", dest="to_backend", choices=["json", "sqlite", "postgres"], required=True)
    parser.add_argument("--to-path", dest="to_path")
    parser.add_argument("--to-host", dest="to_host")
    parser.add_argument("--to-port", dest="to_port", type=int)
    parser.add_argument("--to-database", dest="to_database")
    parser.add_argument("--to-user", dest="to_user")
    parser.add_argument("--to-password", dest="to_password")
    parser.add_argument("--to-base-dir", dest="to_base_dir")
    # Options
    parser.add_argument("--no-verify", dest="verify", action="store_false", default=True)
    parser.add_argument("--batch-size", dest="batch_size", type=int, default=100)
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    source = _build_backend(args, "from")
    target = _build_backend(args, "to")

    if not source or not target:
        parser.error("Could not build source or target backend from args")

    logger.info(f"Source: {source.backend_name()}")
    logger.info(f"Target: {target.backend_name()}")
    logger.info(f"Ping source: {source.ping()}")
    logger.info(f"Ping target: {target.ping()}")

    try:
        report = migrate(source, target, verify=args.verify, batch_size=args.batch_size)
    finally:
        source.close()
        target.close()

    print("\n=== Migration Report ===")
    for key, value in report.items():
        print(f"  {key}: {value}")
    sys.exit(0 if report["verified"] else 1)


if __name__ == "__main__":
    main()