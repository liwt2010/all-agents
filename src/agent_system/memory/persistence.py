"""
JSON 持久化 — Git 友好的多向链接图存盘

节点: data/graph/nodes/{type}/{id}.json
链接: data/graph/links/{year}/{month}.jsonl

参考架构文档 8.6 存盘格式。
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from agent_system.memory.graph import (
    MultiLinkGraph,
    GraphNode,
    GraphLink,
    NodeType,
    LinkType,
)

logger = logging.getLogger(__name__)


def _get_base_dir() -> Path:
    """Get the base data directory"""
    from agent_system.config.settings import get_settings
    settings = get_settings()
    return Path(settings.memory.graph_dir).resolve()


def save_node(node: GraphNode, base_dir: Optional[Path] = None) -> bool:
    """Save a single node to disk"""
    if base_dir is None:
        base_dir = _get_base_dir()
    node_dir = base_dir / "nodes" / node.type.value
    node_dir.mkdir(parents=True, exist_ok=True)
    filepath = node_dir / f"{node.id}.json"
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(node.to_disk_dict(), f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save node {node.id}: {e}")
        return False


def load_node(node_id: str, base_dir: Optional[Path] = None) -> Optional[GraphNode]:
    """Load a single node from disk"""
    if base_dir is None:
        base_dir = _get_base_dir()

    # Search all type directories
    nodes_dir = base_dir / "nodes"
    if not nodes_dir.exists():
        return None

    for type_dir in nodes_dir.iterdir():
        if not type_dir.is_dir():
            continue
        filepath = type_dir / f"{node_id}.json"
        if filepath.exists():
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return GraphNode(**data)
            except Exception as e:
                logger.error(f"Failed to load node {node_id}: {e}")
                return None
    return None


def save_link(link: GraphLink, base_dir: Optional[Path] = None) -> bool:
    """Save a link to the monthly JSONL file"""
    if base_dir is None:
        base_dir = _get_base_dir()

    created = link.created_at
    if isinstance(created, str):
        created = datetime.fromisoformat(created)

    links_dir = base_dir / "links" / str(created.year) / f"{created.month:02d}"
    links_dir.mkdir(parents=True, exist_ok=True)
    filepath = links_dir / "links.jsonl"

    try:
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(link.to_disk_dict(), ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        logger.error(f"Failed to save link: {e}")
        return False


def save_graph(graph: MultiLinkGraph, base_dir: Optional[Path] = None) -> int:
    """Persist entire graph to disk. Returns count of items saved."""
    if base_dir is None:
        base_dir = _get_base_dir()
    base_dir.mkdir(parents=True, exist_ok=True)

    # Collect all nodes and find the minimal set of links not yet persisted
    count = 0
    for node_id in list(graph._nodes.keys()):
        node = graph.get_node(node_id)
        if node:
            if save_node(node, base_dir):
                count += 1

    # Links are appended; for a full save we'd need to dedupe, but for now
    # we record all outgoing links
    for source_id in graph._outgoing:
        for link in graph._outgoing[source_id]:
            if save_link(link, base_dir):
                count += 1

    logger.info(f"Saved {count} items to {base_dir}")
    return count


def load_graph(base_dir: Optional[Path] = None) -> MultiLinkGraph:
    """Load entire graph from disk"""
    if base_dir is None:
        base_dir = _get_base_dir()

    graph = MultiLinkGraph()

    nodes_dir = base_dir / "nodes"
    if nodes_dir.exists():
        for type_dir in sorted(nodes_dir.iterdir()):
            if not type_dir.is_dir():
                continue
            try:
                node_type = NodeType(type_dir.name)
            except ValueError:
                continue
            for json_file in sorted(type_dir.glob("*.json")):
                try:
                    with open(json_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    node = GraphNode(**data)
                    graph._nodes[node.id] = node
                    graph._type_index[node.type].add(node.id)
                except Exception as e:
                    logger.warning(f"Failed to load {json_file}: {e}")

    # Load links from JSONL files
    links_dir = base_dir / "links"
    if links_dir.exists():
        for year_dir in sorted(links_dir.iterdir()):
            if not year_dir.is_dir():
                continue
            for month_dir in sorted(year_dir.iterdir()):
                if not month_dir.is_dir():
                    continue
                jsonl_file = month_dir / "links.jsonl"
                if jsonl_file.exists():
                    try:
                        with open(jsonl_file, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    data = json.loads(line)
                                    link = GraphLink(**data)
                                    graph._outgoing[link.source_id].append(link)
                                    graph._incoming[link.target_id].append(link)
                                except json.JSONDecodeError:
                                    continue
                    except Exception as e:
                        logger.warning(f"Failed to load {jsonl_file}: {e}")

    logger.info(f"Loaded {graph.node_count()} nodes, {graph.link_count()} links from {base_dir}")
    return graph


# ── Archive / vacuum ──

def archive_node_to_disk(node: GraphNode, base_dir: Optional[Path] = None) -> bool:
    """
    Write a node JSON to data/graph/archive/{type}/{id}-{ts}.json.
    The original nodes/ copy is left intact (so we can verify before vacuum).
    """
    if base_dir is None:
        base_dir = _get_base_dir()
    archive_dir = base_dir / "archive" / node.type.value
    archive_dir.mkdir(parents=True, exist_ok=True)
    ts = node.updated_at.strftime("%Y%m%d-%H%M%S")
    filename = f"{node.id}-{ts}.json"
    filepath = archive_dir / filename
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(node.to_disk_dict(), f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to archive node {node.id}: {e}")
        return False


def list_archived(base_dir: Optional[Path] = None) -> list:
    """List all archived node JSON files."""
    if base_dir is None:
        base_dir = _get_base_dir()
    archive_dir = base_dir / "archive"
    if not archive_dir.exists():
        return []
    return [str(p) for p in archive_dir.rglob("*.json")]


def vacuum_archived(retention_days: int = 365, base_dir: Optional[Path] = None) -> int:
    """
    Permanently delete archived nodes older than retention_days.
    Returns count of files deleted.
    """
    from datetime import datetime, timezone, timedelta
    if base_dir is None:
        base_dir = _get_base_dir()
    archive_dir = base_dir / "archive"
    if not archive_dir.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = 0
    for f in list(archive_dir.rglob("*.json")):
        try:
            stat = f.stat()
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
                deleted += 1
        except Exception as e:
            logger.warning(f"Failed to vacuum {f}: {e}")
    return deleted
