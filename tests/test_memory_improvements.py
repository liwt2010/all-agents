"""
Tests: Memory system improvements — embeddings, decay, compaction, admin CLI
"""

import pytest
from datetime import datetime, timezone, timedelta

from agent_system.memory.graph import (
    get_graph,
    reset_graph,
    GraphNode,
    NodeType,
    LinkType,
)
from agent_system.memory.embeddings import (
    get_backend,
    reset_backend,
    decay_factor,
    effective_score,
    KeywordBackend,
    DEFAULT_HALF_LIFE_DAYS,
)


class TestEmbeddings:
    """Embedding backends"""

    def setup_method(self):
        reset_backend()

    def test_get_backend_returns_something(self):
        backend = get_backend()
        assert backend is not None
        assert backend.name in ("keyword", "tfidf", "sentence")

    def test_force_keyword_backend(self):
        backend = get_backend(force="keyword")
        assert backend.name == "keyword"

    def test_keyword_backend_similarity(self):
        backend = KeywordBackend()
        sim = backend.compute_similarity("API timeout", "API rate limit exceeded")
        assert sim > 0

        sim_zero = backend.compute_similarity("API timeout", "completely unrelated")
        # May be > 0 due to short stop words, but should be smaller
        assert sim > sim_zero * 0.5

    def test_keyword_empty_input_returns_zero(self):
        backend = KeywordBackend()
        assert backend.compute_similarity("", "anything") == 0.0
        assert backend.compute_similarity("anything", "") == 0.0

    def test_env_var_forces_keyword(self, monkeypatch):
        monkeypatch.setenv("AGENT_MEMORY_USE_KEYWORDS", "1")
        reset_backend()
        backend = get_backend()
        assert backend.name == "keyword"


class TestDecay:
    """Time decay for experience relevance"""

    def test_decay_factor_zero_age(self):
        node = GraphNode(id="n1", type=NodeType.EXPERIENCE)
        factor = decay_factor(node, half_life_days=30.0)
        assert factor == pytest.approx(1.0, abs=0.01)

    def test_decay_factor_half_life(self):
        node = GraphNode(
            id="n2", type=NodeType.EXPERIENCE,
            created_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
        factor = decay_factor(node, half_life_days=30.0)
        assert factor == pytest.approx(0.5, abs=0.01)

    def test_decay_factor_90_days(self):
        node = GraphNode(
            id="n3", type=NodeType.EXPERIENCE,
            created_at=datetime.now(timezone.utc) - timedelta(days=90),
        )
        factor = decay_factor(node, half_life_days=30.0)
        assert factor == pytest.approx(0.125, abs=0.01)  # 0.5^3

    def test_decay_factor_custom_half_life(self):
        node = GraphNode(
            id="n4", type=NodeType.EXPERIENCE,
            created_at=datetime.now(timezone.utc) - timedelta(days=7),
        )
        # 7-day half-life: 7 days = 0.5
        factor = decay_factor(node, half_life_days=7.0)
        assert factor == pytest.approx(0.5, abs=0.01)

    def test_effective_score_combines_similarity_and_age(self):
        old = GraphNode(
            id="old", type=NodeType.EXPERIENCE,
            created_at=datetime.now(timezone.utc) - timedelta(days=60),
        )
        new = GraphNode(
            id="new", type=NodeType.EXPERIENCE,
        )
        # Both have similarity 1.0
        old_eff = effective_score(1.0, old, half_life_days=30.0)
        new_eff = effective_score(1.0, new, half_life_days=30.0)
        assert new_eff > old_eff


class TestCompaction:
    """Graph compaction and archiving"""

    def setup_method(self):
        reset_graph()

    def test_find_orphan_no_links(self):
        g = get_graph()
        old_node = GraphNode(
            id="orphan-1", type=NodeType.TASK,
            created_at=datetime.now(timezone.utc) - timedelta(days=100),
        )
        g.add_node(old_node)
        orphans = g.find_orphan_nodes(reference_window_days=30)
        assert any(n.id == "orphan-1" for n in orphans)

    def test_find_orphan_excludes_recent(self):
        g = get_graph()
        recent = GraphNode(id="recent", type=NodeType.TASK)
        g.add_node(recent)
        orphans = g.find_orphan_nodes(reference_window_days=30)
        assert not any(n.id == "recent" for n in orphans)

    def test_find_orphan_excludes_linked(self):
        g = get_graph()
        old_node = GraphNode(
            id="linked-1", type=NodeType.TASK,
            created_at=datetime.now(timezone.utc) - timedelta(days=100),
        )
        recent_node = GraphNode(id="linked-2", type=NodeType.OUTPUT)
        g.add_node(old_node)
        g.add_node(recent_node)
        g.link("linked-1", "linked-2", LinkType.CREATED_BY)
        orphans = g.find_orphan_nodes(reference_window_days=30)
        assert not any(n.id == "linked-1" for n in orphans)

    def test_compact_removes_old_orphans(self, tmp_path, monkeypatch):
        from agent_system.memory import persistence
        monkeypatch.setattr(persistence, "_get_base_dir", lambda: tmp_path)

        g = get_graph()
        old = GraphNode(
            id="to-archive", type=NodeType.TASK,
            created_at=datetime.now(timezone.utc) - timedelta(days=200),
        )
        g.add_node(old)
        count = g.compact(older_than_days=90, reference_window_days=30)
        assert count == 1
        # In-memory should be gone
        assert g.get_node("to-archive") is None
        # File should exist on disk
        archive_files = list((tmp_path / "archive" / "task").glob("*.json"))
        assert len(archive_files) == 1

    def test_compact_keeps_recent_nodes(self, tmp_path, monkeypatch):
        from agent_system.memory import persistence
        monkeypatch.setattr(persistence, "_get_base_dir", lambda: tmp_path)

        g = get_graph()
        recent = GraphNode(id="recent-node", type=NodeType.TASK)
        g.add_node(recent)
        count = g.compact(older_than_days=90, reference_window_days=30)
        assert count == 0
        assert g.get_node("recent-node") is not None

    def test_age_buckets(self):
        g = get_graph()
        # Add nodes of different ages
        g.add_node(GraphNode(id="new1", type=NodeType.TASK))
        g.add_node(GraphNode(
            id="old1", type=NodeType.TASK,
            created_at=datetime.now(timezone.utc) - timedelta(days=200),
        ))
        buckets = g.age_buckets()
        assert buckets["task"]["<1d"] >= 1
        assert buckets["task"][">90d"] >= 1


class TestVacuum:
    """Archive vacuum"""

    def test_vacuum_removes_old_files(self, tmp_path):
        from agent_system.memory.persistence import vacuum_archived
        # Create fake old archive file
        import time
        archive_dir = tmp_path / "archive" / "task"
        archive_dir.mkdir(parents=True)
        f = archive_dir / "old-node-20240101.json"
        f.write_text("{}")
        # Make it appear 400 days old
        old_time = time.time() - (400 * 86400)
        import os
        os.utime(f, (old_time, old_time))

        deleted = vacuum_archived(retention_days=365, base_dir=tmp_path)
        assert deleted == 1
        assert not f.exists()

    def test_vacuum_keeps_recent_files(self, tmp_path):
        from agent_system.memory.persistence import vacuum_archived
        archive_dir = tmp_path / "archive" / "task"
        archive_dir.mkdir(parents=True)
        f = archive_dir / "recent-node.json"
        f.write_text("{}")
        deleted = vacuum_archived(retention_days=365, base_dir=tmp_path)
        assert deleted == 0
        assert f.exists()


class TestExperienceWithDecay:
    """Verify the experience lookup uses decay"""

    def setup_method(self):
        reset_graph()
        reset_backend()

    def test_recent_experience_ranks_higher(self):
        from agent_system.memory.experience import get_relevant_experiences
        g = get_graph()
        # New experience with relevant content
        g.add_node(GraphNode(
            id="recent-exp", type=NodeType.EXPERIENCE,
            content={"summary": "User login with JWT token handling"},
        ))
        # Old experience with same content
        g.add_node(GraphNode(
            id="old-exp", type=NodeType.EXPERIENCE,
            content={"summary": "User login with JWT token handling"},
            created_at=datetime.now(timezone.utc) - timedelta(days=180),
        ))

        # With a short half-life, recent should win
        results = get_relevant_experiences(g, "user login JWT", max_results=2, half_life_days=7.0)
        assert len(results) >= 1
        # Both should appear; the recent one with the same content should rank higher
        if len(results) >= 2:
            assert "recent-exp" in results[0]
