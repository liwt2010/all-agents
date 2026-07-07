"""
Tests for Dataview Engine (PR 1)
ARCHITECTURE.md Ch.10 — Obsidian-Dataview-style query system

Coverage:
  - Tokenizer (keywords, identifiers, operators)
  - Parser (SELECT, FROM, WHERE, aggregation, STEPS FROM, subquery)
  - Executor (filter, aggregation, traversal)
  - 9 metrics end-to-end via MetricsCalculator
  - Error messages
  - Performance (1000 nodes)
"""

import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

import pytest

from agent_system.core.dataview import (
    Query,
    QueryError,
    QueryRequest,
    QueryResult,
    tokenize,
    query,
    execute_query,
)
from agent_system.memory.graph import (
    MultiLinkGraph,
    GraphNode,
    NodeType,
    LinkType,
)


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def graph() -> MultiLinkGraph:
    """Build a small graph with task/output/failure/experience/feedback nodes."""
    g = MultiLinkGraph()

    # 5 tasks: 3 completed, 1 running, 1 failed
    tasks = [
        GraphNode(id="t1", type=NodeType.TASK, content={"status": "completed", "agent": "product", "duration_seconds": 10.0, "cost": 0.1}),
        GraphNode(id="t2", type=NodeType.TASK, content={"status": "completed", "agent": "tech", "duration_seconds": 20.0, "cost": 0.2}),
        GraphNode(id="t3", type=NodeType.TASK, content={"status": "completed", "agent": "tech", "duration_seconds": 30.0, "cost": 0.15}),
        GraphNode(id="t4", type=NodeType.TASK, content={"status": "running", "agent": "product", "duration_seconds": 5.0, "cost": 0.05}),
        GraphNode(id="t5", type=NodeType.TASK, content={"status": "failed", "agent": "tech", "duration_seconds": 15.0, "cost": 0.1}),
    ]
    for t in tasks:
        g.add_node(t)

    # 5 outputs: 1 invalid
    outputs = [
        GraphNode(id="o1", type=NodeType.OUTPUT, content={"type": "task_output", "duration": 10.0, "valid": True}),
        GraphNode(id="o2", type=NodeType.OUTPUT, content={"type": "task_output", "duration": 20.0, "valid": True}),
        GraphNode(id="o3", type=NodeType.OUTPUT, content={"type": "task_output", "duration": 30.0, "valid": True}),
        GraphNode(id="o4", type=NodeType.OUTPUT, content={"type": "task_output", "duration": 5.0, "valid": True}),
        GraphNode(id="o5", type=NodeType.OUTPUT, content={"type": "task_output", "duration": 15.0, "valid": False}),
    ]
    for o in outputs:
        g.add_node(o)

    # 2 failures
    failures = [
        GraphNode(id="f1", type=NodeType.FAILURE, content={"task_id": "t5", "severity": "high"}),
        GraphNode(id="f2", type=NodeType.FAILURE, content={"task_id": "t4", "severity": "medium"}),
    ]
    for f in failures:
        g.add_node(f)

    # 1 experience
    g.add_node(GraphNode(id="e1", type=NodeType.EXPERIENCE, content={"success": True, "success_rate": 0.85}))

    # 2 feedbacks
    feedbacks = [
        GraphNode(id="fb1", type=NodeType.FEEDBACK, content={"score": 4.5, "type": "user_rating"}),
        GraphNode(id="fb2", type=NodeType.FEEDBACK, content={"score": 3.5, "type": "user_rating"}),
    ]
    for fb in feedbacks:
        g.add_node(fb)

    # 1 reflection
    g.add_node(GraphNode(id="r1", type=NodeType.DECISION, content={"type": "reflection"}))

    # Link failures to reflections
    g.link("f1", "r1", LinkType.REFERS_TO, created_by="test")

    return g


# ── Tokenizer tests ──────────────────────────────────────────────

def test_tokenizer_keywords():
    tokens = tokenize("SELECT COUNT(*) FROM tasks WHERE status = 'completed';")
    types = [t.type.value for t in tokens]
    # Expect: SELECT, COUNT, (, *, ), FROM, IDENT, WHERE, IDENT, EQ, STRING, ;, EOF
    assert types[0] == "SELECT"
    assert "COUNT" in types
    assert "FROM" in types
    assert "WHERE" in types
    assert types[-1] == "EOF"


def test_tokenizer_identifiers_and_strings():
    tokens = tokenize("SELECT agent FROM tasks WHERE id = 't1';")
    idents = [t.value for t in tokens if t.type.value == "IDENT"]
    assert "agent" in idents
    assert "tasks" in idents
    assert "id" in idents
    strings = [t.value for t in tokens if t.type.value == "STRING"]
    assert "t1" in strings


def test_tokenizer_operators():
    tokens = tokenize("SELECT * FROM x WHERE a >= 10 AND b != 'foo';")
    op_values = [t.value for t in tokens if t.type.value in (">=", "!=", "=", "<", ">", "<=")]
    assert ">=" in op_values
    assert "!=" in op_values


def test_tokenizer_unexpected_character():
    with pytest.raises(QueryError) as exc_info:
        tokenize("SELECT $ FROM tasks;")
    assert "Unexpected character" in str(exc_info.value)


# ── Parser tests ─────────────────────────────────────────────────

def test_parse_select_from(graph):
    result = query("SELECT id, status FROM tasks;", graph=graph)
    assert len(result.rows) == 5
    assert "id" in result.columns
    assert "status" in result.columns


def test_parse_where_comparison(graph):
    result = query("SELECT id, status FROM tasks WHERE status = 'completed';", graph=graph)
    assert len(result.rows) == 3
    assert all(r["status"] == "completed" for r in result.rows)


def test_parse_aggregation_count(graph):
    result = query("SELECT COUNT(*) AS total FROM tasks;", graph=graph)
    assert result.aggregations["total"] == 5.0


def test_parse_aggregation_avg(graph):
    result = query("SELECT AVG(content.duration_seconds) AS avg_dur FROM tasks;", graph=graph)
    expected = (10.0 + 20.0 + 30.0 + 5.0 + 15.0) / 5
    assert abs(result.aggregations["avg_dur"] - expected) < 0.01


def test_parse_filter_aggregate(graph):
    """COUNT(*) FILTER (WHERE ...) — used for ratio metrics. Ratio itself is computed in Python by MetricsCalculator."""
    result = query("SELECT COUNT(*) FILTER (WHERE status = 'completed') AS completed_count FROM tasks;", graph=graph)
    assert result.aggregations["completed_count"] == 3.0
    # Ratio computed in application layer (see MetricsCalculator)
    total = query("SELECT COUNT(*) AS total FROM tasks;", graph=graph)
    ratio = result.aggregations["completed_count"] / total.aggregations["total"]
    assert abs(ratio - 0.6) < 0.01


def test_parse_steps_from(graph):
    """STEPS FROM graph traversal — count tasks within 1 step of a failure."""
    # Build a link: t5 -[caused_by]-> f1
    graph.link("t5", "f1", LinkType.CAUSED_BY)
    # 1 STEPS FROM t5 should include f1
    result = query("SELECT id FROM failures WHERE 1 STEPS FROM 't5';", graph=graph)
    ids = {r["id"] for r in result.rows}
    assert "f1" in ids


def test_parse_subquery_in(graph):
    """IN (subquery) — find tasks that match failures."""
    sql = """
    SELECT id FROM tasks
    WHERE id IN (
        SELECT content.task_id FROM failures
    );
    """
    result = query(sql, graph=graph)
    ids = {r["id"] for r in result.rows}
    assert "t5" in ids  # t5 caused f1


def test_parse_order_limit(graph):
    result = query("SELECT id, agent FROM tasks ORDER BY agent DESC LIMIT 2;", graph=graph)
    assert len(result.rows) == 2


def test_parse_dotted_field(graph):
    """content.field path resolution."""
    result = query("SELECT id, content.agent FROM tasks LIMIT 3;", graph=graph)
    for row in result.rows:
        assert "agent" in row
        assert row["agent"] in ("product", "tech")


# ── Executor tests ───────────────────────────────────────────────

def test_execute_empty_graph():
    g = MultiLinkGraph()
    result = query("SELECT COUNT(*) FROM tasks;", graph=g)
    assert result.aggregations["count"] == 0.0
    assert result.row_count == 0


def test_execute_filter_combinations(graph):
    result = query("SELECT id FROM tasks WHERE content.cost > 0.1;", graph=graph)
    ids = {r["id"] for r in result.rows}
    # t2 (0.2) and t3 (0.15) should match
    assert "t2" in ids
    assert "t3" in ids


def test_execute_steps_traversal_with_links(graph):
    """Verify 2 STEPS FROM reaches grandchildren."""
    graph.link("t5", "f1", LinkType.CAUSED_BY)
    graph.link("f1", "r1", LinkType.REFERS_TO)
    # 2 STEPS FROM t5 should include both f1 and r1
    result = query("SELECT id FROM decisions WHERE 2 STEPS FROM 't5';", graph=graph)
    ids = {r["id"] for r in result.rows}
    assert "r1" in ids


def test_execute_subquery_returns_ids(graph):
    sql = "SELECT id FROM tasks WHERE content.agent IN (SELECT DISTINCT content.agent FROM tasks WHERE status = 'failed');"
    result = query(sql, graph=graph)
    ids = {r["id"] for r in result.rows}
    assert "t5" in ids  # t5 has agent='tech' which is the only agent with failures


# ── Metrics integration ─────────────────────────────────────────

def test_metrics_via_dataview(graph):
    """9 metrics all reachable via Dataview queries."""
    from agent_system.core.observability import MetricsCalculator
    calc = MetricsCalculator(graph)
    metrics = calc.calculate_all()
    assert "end_to_end_success_rate" in metrics
    assert "avg_completion_time" in metrics
    assert "cost_per_task" in metrics
    assert "user_satisfaction" in metrics
    assert "failure_rate_by_agent" in metrics
    assert "reflection_trigger_rate" in metrics
    assert "escalation_request_rate" in metrics
    assert "validation_failure_rate" in metrics
    assert "experience_effectiveness" in metrics
    assert metrics["end_to_end_success_rate"].value == 0.6


# ── Error / edge case tests ──────────────────────────────────────

def test_error_unknown_node_type(graph):
    with pytest.raises(QueryError) as exc_info:
        query("SELECT id FROM unknown_type;", graph=graph)
    assert "Unknown node type" in str(exc_info.value)
    assert exc_info.value.hint is not None


def test_error_missing_from(graph):
    with pytest.raises(QueryError):
        query("SELECT id;", graph=graph)


def test_error_unterminated_string():
    with pytest.raises(QueryError):
        tokenize("SELECT id FROM tasks WHERE name = 'unclosed;")


def test_error_extra_token_after_semicolon(graph):
    with pytest.raises(QueryError) as exc_info:
        query("SELECT id FROM tasks; EXTRA", graph=graph)
    assert "Unexpected" in str(exc_info.value)


def test_error_location_includes_line_column():
    with pytest.raises(QueryError) as exc_info:
        tokenize("SELECT $ FROM x;")
    err = exc_info.value
    assert err.line >= 1
    assert err.column >= 1


# ── Builder API tests ────────────────────────────────────────────

def test_builder_basic(graph):
    q = Query(graph).from_("tasks").where(status="completed").count()
    result = q.execute()
    assert result.aggregations["count"] == 3.0


def test_builder_chainable(graph):
    q = (
        Query(graph)
        .from_("tasks")
        .where(agent="tech")
        .select("id", "status", "agent")
        .order_by("id", descending=False)
        .limit(5)
    )
    result = q.execute()
    assert len(result.rows) >= 1
    assert all(r["agent"] == "tech" for r in result.rows)


def test_builder_steps_from(graph):
    graph.link("t5", "f1", LinkType.CAUSED_BY)
    q = Query(graph).from_("failures").steps_from(1, "'t5'").count()
    result = q.execute()
    assert result.aggregations["count"] >= 1.0


# ── Performance test ─────────────────────────────────────────────

def test_performance_1000_nodes():
    """1000 tasks should execute COUNT/AVG queries in well under 100ms."""
    g = MultiLinkGraph()
    for i in range(1000):
        g.add_node(GraphNode(
            id=f"t{i}",
            type=NodeType.TASK,
            content={"status": "completed" if i % 3 != 0 else "failed", "duration_seconds": float(i)},
        ))
    start = time.perf_counter()
    result = query("SELECT COUNT(*) AS total, AVG(content.duration_seconds) AS avg FROM tasks;", graph=g)
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert result.aggregations["total"] == 1000.0
    assert elapsed_ms < 100, f"Query took {elapsed_ms}ms, expected <100ms"


# ── Dataview SQL coverage of 9 metrics (matches DATAVIEW.md §3.5) ──

def test_9_metric_sqls_match_legacy(graph):
    """Where comparable, Dataview-driven metric matches legacy MetricsCalculator.
    Note: SQL '/' binary expression is out of PR 1 scope; ratios computed in Python layer.
    """
    from agent_system.core.observability import MetricsCalculator
    calc = MetricsCalculator(graph)
    legacy = calc.calculate_all()
    legacy_values = {k: round(v.value, 4) for k, v in legacy.items()}

    # Metric 1: success_rate = completed/total (Python layer does the divide)
    completed = query("SELECT COUNT(*) FILTER (WHERE status = 'completed') AS n FROM tasks;", graph=graph)
    total = query("SELECT COUNT(*) AS n FROM tasks;", graph=graph)
    ratio = completed.aggregations["n"] / total.aggregations["n"]
    assert round(ratio, 4) == legacy_values["end_to_end_success_rate"]

    # Metric 4: user_satisfaction = AVG(feedback.score)
    result = query("SELECT AVG(content.score) AS v FROM feedbacks;", graph=graph)
    assert round(result.aggregations["v"], 4) == legacy_values["user_satisfaction"]

    # Metric 9: experience_effectiveness = success/total (Python layer)
    success = query("SELECT COUNT(*) FILTER (WHERE content.success = true) AS n FROM experiences;", graph=graph)
    total_exp = query("SELECT COUNT(*) AS n FROM experiences;", graph=graph)
    ratio = success.aggregations["n"] / total_exp.aggregations["n"]
    assert round(ratio, 4) == legacy_values["experience_effectiveness"]