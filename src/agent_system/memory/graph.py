"""
MultiLinkGraph — 多向链接图（v13 核心）

超越 Obsidian 双链的 N 向链接图系统。
11 种节点类型、23 种链接类型（初始实现 8+ 种）。
参考架构文档第 8 章。
"""

import json
import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ── 节点类型 ──

class NodeType(str, Enum):
    TASK = "task"
    OUTPUT = "output"
    FAILURE = "failure"
    EXPERIENCE = "experience"
    TOOL = "tool"
    USER = "user"
    PROMPT = "prompt"
    SCHEMA = "schema"
    DECISION = "decision"
    FEEDBACK = "feedback"
    EVENT = "event"


# ── 链接类型（8 类） ──

class LinkType(str, Enum):
    # 内容引用
    REFERS_TO = "refers_to"
    EMBEDS = "embeds"
    # 因果关系
    CAUSED_BY = "caused_by"
    CAUSES = "causes"
    TRIGGERED = "triggered"
    # 演化关系
    EVOLVED_FROM = "evolved_from"
    SUPERSEDES = "supersedes"
    DEPRECATED_BY = "deprecated_by"
    # 协作关系
    DISCUSSED_WITH = "discussed_with"
    HANDED_OFF_TO = "handed_off_to"
    ESCALATED_TO = "escalated_to"
    # 验证关系
    VALIDATED_BY = "validated_by"
    TESTED_BY = "tested_by"
    FAILED_WITH = "failed_with"
    # 知识关系
    REFERENCES = "references"
    BELONGS_TO = "belongs_to"
    PART_OF = "part_of"
    # 创建关系
    CREATED_BY = "created_by"
    MODIFIED_BY = "modified_by"
    APPROVED_BY = "approved_by"
    # 时序关系
    BEFORE = "before"
    AFTER = "after"
    CONCURRENT = "concurrent"


# ── 数据模型 ──

class GraphNode(BaseModel):
    """图节点"""
    id: str
    type: NodeType
    content: Dict[str, Any] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def model_dump_json(self, *args, **kwargs) -> str:
        # Ensure datetime serialization
        return super().model_dump_json(*args, **kwargs)

    def to_disk_dict(self) -> dict:
        d = self.model_dump(mode="json")
        return d


class GraphLink(BaseModel):
    """链接 — 带类型/权重/上下文/时间戳"""
    source_id: str
    target_id: str
    link_type: LinkType
    weight: float = 1.0
    context: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: str = "system"

    def to_disk_dict(self) -> dict:
        return self.model_dump(mode="json")


class NeighborResult(BaseModel):
    """邻居查询结果"""
    node: GraphNode
    link: GraphLink
    depth: int = 1


class PathResult(BaseModel):
    """路径查询结果"""
    found: bool = False
    path: List[Tuple[GraphNode, GraphLink]] = Field(default_factory=list)
    length: int = 0


# ── 主图类 ──

class MultiLinkGraph:
    """多向链接图 — Agent 记忆系统的核心"""

    def __init__(self):
        self._nodes: Dict[str, GraphNode] = {}        # node_id -> node
        self._outgoing: Dict[str, List[GraphLink]] = defaultdict(list)  # source -> links
        self._incoming: Dict[str, List[GraphLink]] = defaultdict(list)  # target -> links
        self._type_index: Dict[NodeType, Set[str]] = defaultdict(set)   # type -> node_ids

    # ── 节点操作 ──

    def add_node(self, node: GraphNode) -> str:
        """添加一个节点。如果 id 已存在则更新（updated_at 刷新）"""
        existing = self._nodes.get(node.id)
        if existing:
            # For existing nodes, don't overwrite created_at
            node.created_at = existing.created_at
        node.updated_at = datetime.now(timezone.utc)
        self._nodes[node.id] = node
        self._type_index[node.type].add(node.id)
        logger.debug(f"Graph: added/updated node {node.id} ({node.type.value})")
        return node.id

    def get_node(self, node_id: str) -> Optional[GraphNode]:
        return self._nodes.get(node_id)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def find_nodes(
        self,
        node_type: Optional[NodeType] = None,
        **filters,
    ) -> List[GraphNode]:
        """按类型和可选的 content 字段过滤查找节点"""
        if node_type:
            ids = self._type_index.get(node_type, set())
            candidates = [self._nodes[nid] for nid in ids if nid in self._nodes]
        else:
            candidates = list(self._nodes.values())

        if not filters:
            return candidates

        results = []
        for node in candidates:
            match = True
            for key, value in filters.items():
                if key.startswith("content."):
                    # content.field 语法
                    field = key[8:]
                    if node.content.get(field) != value:
                        match = False
                        break
                elif key.startswith("metadata."):
                    field = key[9:]
                    if node.metadata.get(field) != value:
                        match = False
                        break
                else:
                    # 顶层字段
                    if getattr(node, key, None) != value:
                        match = False
                        break
            if match:
                results.append(node)

        return results

    def delete_node(self, node_id: str) -> bool:
        """删除节点及其所有链接"""
        if node_id not in self._nodes:
            return False
        node = self._nodes.pop(node_id)
        self._type_index[node.type].discard(node_id)
        # Remove all links to/from this node
        self._outgoing.pop(node_id, None)
        self._incoming.pop(node_id, None)
        # Remove links from other nodes that target this one
        for source_id in list(self._outgoing.keys()):
            self._outgoing[source_id] = [
                l for l in self._outgoing[source_id]
                if l.target_id != node_id
            ]
        for target_id in list(self._incoming.keys()):
            self._incoming[target_id] = [
                l for l in self._incoming[target_id]
                if l.source_id != node_id
            ]
        return True

    def node_count(self) -> int:
        return len(self._nodes)

    # ── Compaction / archiving ──

    def find_orphan_nodes(
        self,
        reference_window_days: int = 30,
        exclude_types: Optional[list] = None,
    ) -> list:
        """
        Find nodes that:
          - Have no incoming AND no outgoing links in the recent window
          - Are older than `reference_window_days`
          - Are not in exclude_types
        """
        from datetime import datetime, timezone, timedelta
        exclude_types = exclude_types or []
        cutoff = datetime.now(timezone.utc) - timedelta(days=reference_window_days)
        orphans = []

        for node in list(self._nodes.values()):
            if node.type in exclude_types:
                continue
            if node.created_at > cutoff:
                continue  # not old enough
            if self._outgoing.get(node.id) or self._incoming.get(node.id):
                continue  # has links
            orphans.append(node)
        return orphans

    def compact(
        self,
        older_than_days: int = 90,
        reference_window_days: int = 30,
        exclude_types: Optional[list] = None,
    ) -> int:
        """
        Find orphan nodes and archive them. Returns count archived.
        Archiving removes from in-memory graph and writes to data/graph/archive/.
        """
        orphans = self.find_orphan_nodes(
            reference_window_days=reference_window_days,
            exclude_types=exclude_types,
        )
        if not orphans:
            return 0

        # Apply stricter age filter
        from datetime import datetime, timezone, timedelta
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        to_archive = [n for n in orphans if n.created_at < cutoff]

        count = 0
        for node in to_archive:
            if self.archive_node(node.id):
                count += 1
        return count

    def archive_node(self, node_id: str) -> bool:
        """
        Archive a node: remove from in-memory graph, write to data/graph/archive/.
        Returns True on success.
        """
        if node_id not in self._nodes:
            return False
        node = self._nodes[node_id]

        # Persist before removing
        try:
            from agent_system.memory.persistence import archive_node_to_disk
            archive_node_to_disk(node)
        except Exception as e:
            logger.warning(f"Failed to archive node {node_id} to disk: {e}")
            return False

        # Remove from in-memory graph (same as delete_node)
        self._nodes.pop(node_id, None)
        self._type_index[node.type].discard(node_id)
        self._outgoing.pop(node_id, None)
        self._incoming.pop(node_id, None)
        for source_id in list(self._outgoing.keys()):
            self._outgoing[source_id] = [
                l for l in self._outgoing[source_id] if l.target_id != node_id
            ]
        for target_id in list(self._incoming.keys()):
            self._incoming[target_id] = [
                l for l in self._incoming[target_id] if l.source_id != node_id
            ]
        return True

    def age_buckets(self) -> Dict[str, Dict[str, int]]:
        """
        Return age distribution per node type. Useful for admin dashboards.
        Buckets: <1d, 1-7d, 7-30d, 30-90d, >90d
        """
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        buckets = {
            "task": {"<1d": 0, "1-7d": 0, "7-30d": 0, "30-90d": 0, ">90d": 0},
            "output": {"<1d": 0, "1-7d": 0, "7-30d": 0, "30-90d": 0, ">90d": 0},
            "failure": {"<1d": 0, "1-7d": 0, "7-30d": 0, "30-90d": 0, ">90d": 0},
            "experience": {"<1d": 0, "1-7d": 0, "7-30d": 0, "30-90d": 0, ">90d": 0},
            "other": {"<1d": 0, "1-7d": 0, "7-30d": 0, "30-90d": 0, ">90d": 0},
        }
        bucket_thresholds = {
            "<1d": timedelta(days=1),
            "1-7d": timedelta(days=7),
            "7-30d": timedelta(days=30),
            "30-90d": timedelta(days=90),
        }
        for node in self._nodes.values():
            t = node.type.value if node.type.value in buckets else "other"
            created = node.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = now - created
            placed = False
            for label, thresh in bucket_thresholds.items():
                if age < thresh:
                    buckets[t][label] += 1
                    placed = True
                    break
            if not placed:
                buckets[t][">90d"] += 1
        return buckets

    # ── 链接操作 ──

    def link(
        self,
        source_id: str,
        target_id: str,
        link_type: LinkType,
        weight: float = 1.0,
        context: Optional[Dict[str, Any]] = None,
        created_by: str = "system",
    ) -> bool:
        """在两个节点之间建立链接。两个节点都必须存在。"""
        if source_id not in self._nodes:
            logger.warning(f"Link failed: source {source_id} not found")
            return False
        if target_id not in self._nodes:
            logger.warning(f"Link failed: target {target_id} not found")
            return False

        link = GraphLink(
            source_id=source_id,
            target_id=target_id,
            link_type=link_type,
            weight=weight,
            context=context or {},
            created_at=datetime.now(timezone.utc),
            created_by=created_by,
        )
        self._outgoing[source_id].append(link)
        self._incoming[target_id].append(link)
        logger.debug(f"Graph: linked {source_id} -[{link_type.value}]-> {target_id}")
        return True

    def get_outgoing(self, node_id: str, link_type: Optional[LinkType] = None) -> List[GraphLink]:
        """获取节点的出边"""
        links = self._outgoing.get(node_id, [])
        if link_type:
            return [l for l in links if l.link_type == link_type]
        return links

    def get_incoming(self, node_id: str, link_type: Optional[LinkType] = None) -> List[GraphLink]:
        """获取节点的入边"""
        links = self._incoming.get(node_id, [])
        if link_type:
            return [l for l in links if l.link_type == link_type]
        return links

    def get_links_between(
        self, source_id: str, target_id: str,
    ) -> List[GraphLink]:
        """获取两个节点之间的所有链接"""
        outgoing = self._outgoing.get(source_id, [])
        return [l for l in outgoing if l.target_id == target_id]

    def link_count(self) -> int:
        # Count unique links (sum of all outgoing)
        return sum(len(links) for links in self._outgoing.values())

    def delete_links(self, source_id: str, target_id: str, link_type: Optional[LinkType] = None) -> int:
        """删除两节点间的链接。返回删除数量。"""
        before = len(self._outgoing.get(source_id, []))
        self._outgoing[source_id] = [
            l for l in self._outgoing.get(source_id, [])
            if not (l.target_id == target_id and (link_type is None or l.link_type == link_type))
        ]
        self._incoming[target_id] = [
            l for l in self._incoming.get(target_id, [])
            if not (l.source_id == source_id and (link_type is None or l.link_type == link_type))
        ]
        after = len(self._outgoing.get(source_id, []))
        return before - after

    # ── 高级查询 ──

    def neighbors(self, node_id: str, depth: int = 1, max_depth: int = 3) -> List[NeighborResult]:
        """N 步邻居查询"""
        if depth > max_depth:
            return []
        if node_id not in self._nodes:
            return []

        visited = {node_id}
        results = []
        queue: deque = deque([(node_id, 0)])

        while queue:
            current, current_depth = queue.popleft()
            if current_depth >= depth:
                continue

            # 出边
            for link in self._outgoing.get(current, []):
                if link.target_id not in visited:
                    visited.add(link.target_id)
                    target_node = self._nodes.get(link.target_id)
                    if target_node:
                        results.append(NeighborResult(
                            node=target_node,
                            link=link,
                            depth=current_depth + 1,
                        ))
                    if current_depth + 1 < depth:
                        queue.append((link.target_id, current_depth + 1))

            # 入边
            for link in self._incoming.get(current, []):
                if link.source_id not in visited:
                    visited.add(link.source_id)
                    source_node = self._nodes.get(link.source_id)
                    if source_node:
                        results.append(NeighborResult(
                            node=source_node,
                            link=link,
                            depth=current_depth + 1,
                        ))
                    if current_depth + 1 < depth:
                        queue.append((link.source_id, current_depth + 1))

        return results

    def path(self, source_id: str, target_id: str, max_depth: int = 10) -> PathResult:
        """BFS 查找两节点间的最短路径"""
        if source_id not in self._nodes or target_id not in self._nodes:
            return PathResult(found=False)

        if source_id == target_id:
            return PathResult(found=True, length=0)

        visited = {source_id}
        queue: deque = deque([(source_id, [])])

        while queue:
            current, path_links = queue.popleft()

            # 检查出边
            for link in self._outgoing.get(current, []):
                if link.target_id == target_id:
                    full_path = path_links + [(self._nodes[current], link), (self._nodes[target_id], link)]
                    return PathResult(
                        found=True,
                        path=full_path,
                        length=len(path_links) + 1,
                    )
                if link.target_id not in visited:
                    visited.add(link.target_id)
                    queue.append((link.target_id, path_links + [(self._nodes[current], link)]))

            # 检查入边
            for link in self._incoming.get(current, []):
                if link.source_id == target_id:
                    full_path = path_links + [(self._nodes[current], link), (self._nodes[target_id], link)]
                    return PathResult(
                        found=True,
                        path=full_path,
                        length=len(path_links) + 1,
                    )
                if link.source_id not in visited:
                    visited.add(link.source_id)
                    queue.append((link.source_id, path_links + [(self._nodes[current], link)]))

            if len(visited) > 10000:
                break

        return PathResult(found=False)

    def related_with_context(self, node_id: str, max_depth: int = 2) -> Dict[str, Any]:
        """获取节点及相关节点的完整上下文"""
        node = self.get_node(node_id)
        if not node:
            return {"node": None, "neighbors": [], "path_to_experience": []}

        neighbors = self.neighbors(node_id, depth=max_depth)

        # Find paths to experience nodes
        experience_paths = []
        for n in neighbors:
            if n.node.type == NodeType.EXPERIENCE:
                p = self.path(node_id, n.node.id)
                if p.found:
                    experience_paths.append(p)

        return {
            "node": node,
            "neighbors": neighbors,
            "path_to_experience": experience_paths,
            "outgoing_count": len(self._outgoing.get(node_id, [])),
            "incoming_count": len(self._incoming.get(node_id, [])),
        }

    # ── 统计 ──

    def stats(self) -> Dict[str, Any]:
        """图统计"""
        type_counts = {}
        for ntype, ids in self._type_index.items():
            type_counts[ntype.value] = len(ids)

        link_type_counts = {}
        for links in self._outgoing.values():
            for link in links:
                lt = link.link_type.value
                link_type_counts[lt] = link_type_counts.get(lt, 0) + 1

        return {
            "total_nodes": len(self._nodes),
            "total_links": self.link_count(),
            "nodes_by_type": type_counts,
            "links_by_type": link_type_counts,
        }


# ── 全局图实例 ──

_graph: Optional[MultiLinkGraph] = None


def get_graph() -> MultiLinkGraph:
    """获取全局图实例"""
    global _graph
    if _graph is None:
        _graph = MultiLinkGraph()
    return _graph


def reset_graph():
    """重置图（测试用）"""
    global _graph
    _graph = MultiLinkGraph()
    return _graph
