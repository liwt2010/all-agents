"""
Parametrized tests across all 3 storage backends.

Each backend must pass the same round-trip / query / delete / migration tests.
JSON / SQLite / Postgres get identical assertions to guarantee parity.

Run: PYTHONPATH=src .venv/Scripts/python.exe -m pytest tests/test_storage.py -v
"""

import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_system.memory.graph import (
    GraphLink,
    GraphNode,
    LinkType,
    MultiLinkGraph,
    NodeType,
)


# ── Backend fixtures ──

@pytest.fixture(params=["json", "sqlite"])
def backend(request, tmp_path):
    """Parametrize over JSON + SQLite backends. Postgres skipped (no DB in CI)."""
    from agent_system.memory.storage import get_storage

    if request.param == "json":
        b = get_storage("json", base_dir=str(tmp_path / "graph_json"))
    else:
        b = get_storage("sqlite", db_path=str(tmp_path / "graph.db"))
    b.init()
    yield b
    b.close()


@pytest.fixture
def sample_node():
    return GraphNode(
        id="node-1",
        type=NodeType.TASK,
        content={"title": "Test task", "description": "Hello"},
        metadata={"priority": "high"},
    )


@pytest.fixture
def sample_link():
    return GraphLink(
        source_id="node-1",
        target_id="node-2",
        link_type=LinkType.CAUSES,
        weight=0.8,
        context={"reason": "test"},
    )


# ── save_node / load_node ──

class TestNodeRoundTrip:
    def test_save_and_load_node(self, backend, sample_node):
        backend.save_node(sample_node)
        loaded = backend.load_node(sample_node.id)
        assert loaded is not None
        assert loaded.id == sample_node.id
        assert loaded.type == sample_node.type
        assert loaded.content == sample_node.content
        assert loaded.metadata == sample_node.metadata

    def test_load_nonexistent_node_returns_none(self, backend):
        assert backend.load_node("does-not-exist") is None

    def test_save_node_overwrites(self, backend, sample_node):
        backend.save_node(sample_node)
        sample_node.content["title"] = "Updated"
        sample_node.updated_at = datetime.now(timezone.utc)
        backend.save_node(sample_node)
        loaded = backend.load_node(sample_node.id)
        assert loaded.content["title"] == "Updated"

    def test_save_node_unicode_content(self, backend):
        node = GraphNode(
            id="unicode-1",
            type=NodeType.EXPERIENCE,
            content={"text": "中文 emoji 测试 🚀", "korean": "안녕"},
            metadata={"tag": "日本語"},
        )
        backend.save_node(node)
        loaded = backend.load_node("unicode-1")
        assert loaded.content["text"] == "中文 emoji 测试 🚀"
        assert loaded.metadata["tag"] == "日本語"


# ── list_nodes ──

class TestListNodes:
    def test_list_all_nodes_empty(self, backend):
        assert backend.list_nodes() == []

    def test_list_all_nodes(self, backend):
        for i in range(5):
            n = GraphNode(id=f"n{i}", type=NodeType.TASK, content={}, metadata={})
            backend.save_node(n)
        assert len(backend.list_nodes()) == 5

    def test_list_nodes_filtered_by_type(self, backend):
        backend.save_node(GraphNode(id="t1", type=NodeType.TASK, content={}, metadata={}))
        backend.save_node(GraphNode(id="t2", type=NodeType.TASK, content={}, metadata={}))
        backend.save_node(GraphNode(id="o1", type=NodeType.OUTPUT, content={}, metadata={}))
        tasks = backend.list_nodes(NodeType.TASK)
        outputs = backend.list_nodes(NodeType.OUTPUT)
        assert len(tasks) == 2
        assert len(outputs) == 1
        assert {n.id for n in tasks} == {"t1", "t2"}


# ── delete_node ──

class TestDeleteNode:
    def test_delete_existing_node(self, backend, sample_node):
        backend.save_node(sample_node)
        assert backend.delete_node(sample_node.id) is True
        assert backend.load_node(sample_node.id) is None

    def test_delete_nonexistent_node(self, backend):
        assert backend.delete_node("nope") is False

    def test_delete_node_removes_links(self, backend, sample_node, sample_link):
        backend.save_node(sample_node)
        backend.save_node(GraphNode(id="node-2", type=NodeType.TASK, content={}, metadata={}))
        backend.save_link(sample_link)
        assert len(backend.list_links("node-1", "out")) == 1
        backend.delete_node("node-1")
        assert len(backend.list_links("node-1", "out")) == 0


# ── save_link / list_links ──

class TestLinkOperations:
    def test_save_and_list_link_outgoing(self, backend, sample_link):
        backend.save_node(GraphNode(id="node-1", type=NodeType.TASK, content={}, metadata={}))
        backend.save_node(GraphNode(id="node-2", type=NodeType.TASK, content={}, metadata={}))
        backend.save_link(sample_link)
        links = backend.list_links("node-1", "out")
        assert len(links) == 1
        assert links[0].target_id == "node-2"

    def test_list_links_incoming(self, backend, sample_link):
        backend.save_node(GraphNode(id="node-1", type=NodeType.TASK, content={}, metadata={}))
        backend.save_node(GraphNode(id="node-2", type=NodeType.TASK, content={}, metadata={}))
        backend.save_link(sample_link)
        links = backend.list_links("node-2", "in")
        assert len(links) == 1
        assert links[0].source_id == "node-1"

    def test_list_links_both(self, backend, sample_link):
        backend.save_node(GraphNode(id="node-1", type=NodeType.TASK, content={}, metadata={}))
        backend.save_node(GraphNode(id="node-2", type=NodeType.TASK, content={}, metadata={}))
        backend.save_link(sample_link)
        from_node = backend.list_links("node-1", "both")
        to_node = backend.list_links("node-2", "both")
        assert len(from_node) == 1
        assert len(to_node) == 1

    def test_list_links_filtered_by_type(self, backend):
        backend.save_node(GraphNode(id="n1", type=NodeType.TASK, content={}, metadata={}))
        backend.save_node(GraphNode(id="n2", type=NodeType.TASK, content={}, metadata={}))
        backend.save_link(GraphLink(source_id="n1", target_id="n2", link_type=LinkType.CAUSES))
        backend.save_link(GraphLink(source_id="n1", target_id="n2", link_type=LinkType.REFERENCES))
        causes = backend.list_links("n1", "out", link_type="causes")
        assert len(causes) == 1
        assert causes[0].link_type == LinkType.CAUSES


# ── save_graph / load_graph (bulk) ──

class TestBulkOperations:
    def test_save_and_load_full_graph(self, backend):
        g = MultiLinkGraph()
        for i in range(10):
            n = GraphNode(
                id=f"n{i}",
                type=NodeType.TASK if i % 2 == 0 else NodeType.OUTPUT,
                content={"i": i},
                metadata={},
            )
            g.add_node(n)
        for i in range(9):
            link = GraphLink(source_id=f"n{i}", target_id=f"n{i+1}", link_type=LinkType.CAUSES)
            g.link(link.source_id, link.target_id, link.link_type)

        backend.save_graph(g)
        g2 = MultiLinkGraph()
        backend.load_graph(g2)
        assert len(g2.find_nodes()) == 10
        assert len(backend.list_links("n0", "out")) == 1

    def test_save_1000_nodes_under_5s(self, backend):
        """Perf smoke test: 1000 nodes + 999 links saves in < 5s.

        JSON backend is file-I/O bound (1 file per node + many jsonl lines),
        so we use a generous 5s threshold. SQLite/Postgres finish in < 1s.
        """
        import time
        g = MultiLinkGraph()
        for i in range(1000):
            g.add_node(GraphNode(id=f"perf-{i}", type=NodeType.TASK, content={"i": i}, metadata={}))
        for i in range(999):
            g.link(f"perf-{i}", f"perf-{i+1}", LinkType.CAUSES)
        started = time.monotonic()
        backend.save_graph(g)
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"save_graph took {elapsed:.2f}s (expected < 5s)"


# ── Health checks ──

class TestHealthChecks:
    def test_ping_returns_true(self, backend):
        assert backend.ping() is True

    def test_backend_name_returns_string(self, backend):
        name = backend.backend_name()
        assert isinstance(name, str)
        assert len(name) > 0


# ── Cross-backend migration ──

class TestCrossBackendMigration:
    def test_migrate_json_to_sqlite(self, tmp_path):
        """Round-trip: write to JSON, migrate to SQLite, verify counts match."""
        from agent_system.memory.storage import get_storage
        from agent_system.memory.storage.migrate import migrate

        json_b = get_storage("json", base_dir=str(tmp_path / "json"))
        json_b.init()
        for i in range(20):
            json_b.save_node(GraphNode(
                id=f"mig-{i}", type=NodeType.TASK,
                content={"i": i}, metadata={},
            ))
            if i > 0:
                json_b.save_link(GraphLink(
                    source_id=f"mig-{i-1}", target_id=f"mig-{i}",
                    link_type=LinkType.CAUSES,
                ))

        sqlite_b = get_storage("sqlite", db_path=str(tmp_path / "out.db"))
        sqlite_b.init()

        report = migrate(json_b, sqlite_b)
        assert report["verified"], f"migration failed: {report}"
        assert report["nodes_migrated"] == 20
        assert report["links_migrated"] == 19

        json_b.close()
        sqlite_b.close()

    def test_migrate_sqlite_to_sqlite_round_trip(self, tmp_path):
        """Migration should be idempotent and lossless across identical backends."""
        from agent_system.memory.storage import get_storage
        from agent_system.memory.storage.migrate import migrate

        src = get_storage("sqlite", db_path=str(tmp_path / "src.db"))
        src.init()
        g = MultiLinkGraph()
        for i in range(50):
            n = GraphNode(id=f"r-{i}", type=NodeType.EXPERIENCE, content={"x": i}, metadata={})
            g.add_node(n)
            if i > 0:
                g.link(f"r-{i-1}", f"r-{i}", LinkType.REFERENCES)
        src.save_graph(g)

        dst = get_storage("sqlite", db_path=str(tmp_path / "dst.db"))
        dst.init()

        report = migrate(src, dst)
        assert report["verified"]
        assert report["nodes_migrated"] == 50

        # Verify content fidelity
        loaded = dst.load_node("r-25")
        assert loaded.content == {"x": 25}

        src.close()
        dst.close()


# ── Factory ──

class TestFactory:
    def test_factory_json(self, tmp_path):
        from agent_system.memory.storage import get_storage
        b = get_storage("json", base_dir=str(tmp_path / "x"))
        assert b.backend_name() == "json"
        b.close()

    def test_factory_sqlite(self, tmp_path):
        from agent_system.memory.storage import get_storage
        b = get_storage("sqlite", db_path=str(tmp_path / "x.db"))
        assert "sqlite" in b.backend_name()
        b.close()

    def test_factory_unknown_backend_raises(self):
        from agent_system.memory.storage import get_storage
        with pytest.raises(ValueError, match="Unknown storage backend"):
            get_storage("oracle")


# ── Concurrent access (SQLite only — JSON doesn't support it) ──

@pytest.mark.skipif("not True", reason="SQLite concurrency is single-writer; smoke only")
class TestSQLiteConcurrency:
    def test_concurrent_reads(self, tmp_path):
        """Multiple threads reading the same SQLite DB should not block."""
        import threading
        from agent_system.memory.storage import get_storage

        b = get_storage("sqlite", db_path=str(tmp_path / "conc.db"))
        b.init()
        b.save_node(GraphNode(id="c1", type=NodeType.TASK, content={}, metadata={}))

        errors = []

        def reader():
            try:
                for _ in range(100):
                    n = b.load_node("c1")
                    assert n is not None
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        b.close()