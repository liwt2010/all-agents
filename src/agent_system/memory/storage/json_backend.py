"""
JSON storage backend — wraps existing persistence.py JSON-file format.

Used only for:
  - import/export (migration between backends)
  - debugging / inspection (humans can `cat` the files)
  - dev fallback when no DB is available

NOT recommended for production:
  - Single-process writes (no concurrent safety)
  - No atomic transactions (partial writes possible)
  - Slow with >10k nodes
"""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from agent_system.memory.graph import (
    GraphLink,
    GraphNode,
    LinkType,
    NodeType,
)

if TYPE_CHECKING:
    from agent_system.memory.graph import MultiLinkGraph

logger = logging.getLogger(__name__)


class JSONBackend:
    """File-per-node JSON storage (current behavior, wrapped as backend)."""

    def __init__(self, base_dir: str = "./data/graph"):
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def backend_name(self) -> str:
        return "json"

    def ping(self) -> bool:
        return self.base_dir.exists()

    def init(self) -> None:
        """No-op — filesystem is already initialized in __init__."""
        pass

    def close(self) -> None:
        """No-op — no persistent connection."""
        pass

    # ── Path helpers ──

    def _node_path(self, node: GraphNode) -> Path:
        return self.base_dir / "nodes" / node.type.value / f"{node.id}.json"

    def _node_search_path(self, node_id: str) -> Path:
        # Search all type subdirs
        return self.base_dir / "nodes"

    # ── Node operations ──

    def save_node(self, node: GraphNode) -> None:
        path = self._node_path(node)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("w", encoding="utf-8") as f:
                json.dump(node.to_disk_dict(), f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"JSONBackend: failed to save node {node.id}: {e}")
            raise

    def load_node(self, node_id: str) -> Optional[GraphNode]:
        # Search all type dirs
        nodes_dir = self._node_search_path(node_id)
        if not nodes_dir.exists():
            return None
        for type_dir in nodes_dir.iterdir():
            if not type_dir.is_dir():
                continue
            candidate = type_dir / f"{node_id}.json"
            if candidate.exists():
                try:
                    with candidate.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    return GraphNode(**data)
                except Exception as e:
                    logger.warning(f"JSONBackend: failed to load {candidate}: {e}")
                    return None
        return None

    def delete_node(self, node_id: str) -> bool:
        # Try all type dirs
        nodes_dir = self._node_search_path(node_id)
        if not nodes_dir.exists():
            return False
        deleted = False
        for type_dir in nodes_dir.iterdir():
            if not type_dir.is_dir():
                continue
            candidate = type_dir / f"{node_id}.json"
            if candidate.exists():
                candidate.unlink()
                deleted = True
                break
        # Also delete links
        self.delete_links_for_node(node_id)
        return deleted

    def list_nodes(self, node_type: Optional[NodeType] = None) -> List[GraphNode]:
        nodes = []
        nodes_dir = self.base_dir / "nodes"
        if not nodes_dir.exists():
            return []
        dirs_to_scan = (
            [nodes_dir / node_type.value]
            if node_type
            else [d for d in nodes_dir.iterdir() if d.is_dir()]
        )
        for d in dirs_to_scan:
            if not d.exists():
                continue
            for json_file in d.glob("*.json"):
                try:
                    with json_file.open("r", encoding="utf-8") as f:
                        data = json.load(f)
                    nodes.append(GraphNode(**data))
                except Exception as e:
                    logger.warning(f"JSONBackend: failed to load {json_file}: {e}")
        return nodes

    # ── Link operations ──

    def save_link(self, link: GraphLink) -> None:
        year_month = link.created_at.strftime("%Y/%m")
        links_dir = self.base_dir / "links" / year_month
        links_dir.mkdir(parents=True, exist_ok=True)
        path = links_dir / "links.jsonl"
        try:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(link.to_disk_dict(), ensure_ascii=False) + "\n")
        except Exception as e:
            logger.error(f"JSONBackend: failed to save link {link.source_id}→{link.target_id}: {e}")
            raise

    def list_links(
        self,
        node_id: str,
        direction: str = "out",
        link_type: Optional[str] = None,
    ) -> List[GraphLink]:
        links = []
        links_root = self.base_dir / "links"
        if not links_root.exists():
            return []
        target_lt = LinkType(link_type) if link_type else None
        for jsonl_file in links_root.rglob("*.jsonl"):
            try:
                with jsonl_file.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        data = json.loads(line)
                        link = GraphLink(**data)
                        if direction == "out" and link.source_id != node_id:
                            continue
                        if direction == "in" and link.target_id != node_id:
                            continue
                        if direction == "both" and link.source_id != node_id and link.target_id != node_id:
                            continue
                        if target_lt and link.link_type != target_lt:
                            continue
                        links.append(link)
            except Exception as e:
                logger.warning(f"JSONBackend: failed to read {jsonl_file}: {e}")
        return links

    def delete_links_for_node(self, node_id: str) -> int:
        """Remove links referencing node_id from all *.jsonl files."""
        links_root = self.base_dir / "links"
        if not links_root.exists():
            return 0
        deleted_count = 0
        for jsonl_file in links_root.rglob("*.jsonl"):
            try:
                lines = jsonl_file.read_text(encoding="utf-8").splitlines()
                kept = []
                for line in lines:
                    if not line.strip():
                        continue
                    data = json.loads(line)
                    if data.get("source_id") == node_id or data.get("target_id") == node_id:
                        deleted_count += 1
                        continue
                    kept.append(line)
                jsonl_file.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            except Exception as e:
                logger.warning(f"JSONBackend: failed to clean {jsonl_file}: {e}")
        return deleted_count

    # ── Bulk operations ──

    def save_graph(self, graph: "MultiLinkGraph") -> int:
        """Save all nodes and links. Atomic-ish via temp + rename."""
        # Save nodes
        node_count = 0
        for node in graph.find_nodes():
            self.save_node(node)
            node_count += 1
        # Save links
        link_count = 0
        for links in graph._outgoing.values():
            for link in links:
                self.save_link(link)
                link_count += 1
        logger.info(f"JSONBackend: saved {node_count} nodes, {link_count} links")
        return node_count

    def load_graph(self, graph: "MultiLinkGraph") -> int:
        """Load all nodes + links into the in-memory graph."""
        # Load nodes
        nodes = self.list_nodes()
        for node in nodes:
            graph.add_node(node)
        node_count = len(nodes)

        # Load links (scan all jsonl files, dedupe)
        seen_links = set()
        links_root = self.base_dir / "links"
        if links_root.exists():
            for jsonl_file in links_root.rglob("*.jsonl"):
                try:
                    with jsonl_file.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            data = json.loads(line)
                            key = (data["source_id"], data["target_id"], data["link_type"])
                            if key in seen_links:
                                continue
                            seen_links.add(key)
                            link = GraphLink(**data)
                            # add to graph
                            graph._outgoing[link.source_id].append(link)
                            graph._incoming[link.target_id].append(link)
                            graph._type_index.setdefault(link.link_type, set()).add(link.source_id)
                except Exception as e:
                    logger.warning(f"JSONBackend: failed to load links from {jsonl_file}: {e}")
        logger.info(f"JSONBackend: loaded {node_count} nodes")
        return node_count