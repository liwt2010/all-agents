"""
Tests: Iteration 6 — Quotas, Cost Tracking, Observability
"""

import pytest
import time
from datetime import date

from agent_system.core.quota import (
    QuotaManager,
    QuotaAction,
    QuotaLevel,
    QuotaLimit,
    UsageRecord,
    DEFAULT_QUOTAS,
)
from agent_system.core.cost_tracker import (
    CostTracker,
    estimate_cost,
    LLMCallRecord,
    MODEL_PRICING,
)
from agent_system.core.observability import (
    MetricsCalculator,
    SimpleTracer,
    MetricValue,
)
from agent_system.memory.graph import (
    get_graph,
    reset_graph,
    GraphNode,
    NodeType,
    LinkType,
)


class TestQuotaSystem:
    """Test 4-level quota system"""

    def setup_method(self):
        self.qm = QuotaManager()

    def test_default_limits_exist(self):
        assert QuotaLevel.USER in DEFAULT_QUOTAS
        assert QuotaLevel.DEPARTMENT in DEFAULT_QUOTAS
        assert QuotaLevel.SYSTEM in DEFAULT_QUOTAS
        assert QuotaLevel.LLM_API in DEFAULT_QUOTAS

    def test_allow_when_under_quota(self):
        action, reason = self.qm.check_quota("user_1")
        assert action == QuotaAction.ALLOW

    def test_concurrent_limit_user(self):
        """Test user-level concurrent task limit"""
        for _ in range(5):
            action, reason = self.qm.check_quota("busy_user")
            assert action == QuotaAction.ALLOW
            self.qm.store.start_task("busy_user")

        # 6th should be queued
        action, reason = self.qm.check_quota("busy_user")
        assert action == QuotaAction.QUEUE

    def test_concurrent_completes_frees_slot(self):
        for _ in range(5):
            self.qm.store.start_task("user_a")
        action, _ = self.qm.check_quota("user_a")
        assert action == QuotaAction.QUEUE

        # End one task -> should allow
        self.qm.store.end_task("user_a")
        action, _ = self.qm.check_quota("user_a")
        assert action == QuotaAction.ALLOW

    def test_daily_cost_limit(self):
        """User daily cost limit reached -> REJECT"""
        # Exceed the user's daily cost limit by recording usage
        expensive = UsageRecord(
            user_id="poor_user",
            cost=5.0,  # max is 5.0
            tokens_input=1000,
            tokens_output=500,
            action="llm_call",
        )
        self.qm.store.record_usage(expensive)

        action, reason = self.qm.check_quota("poor_user", estimated_cost=0.5)
        assert action == QuotaAction.REJECT
        assert "cost limit" in reason

    def test_token_limit_triggers_downgrade(self):
        """Daily token limit -> DOWNGRADE"""
        big_usage = UsageRecord(
            user_id="heavy_user",
            tokens_input=1_000_000,
            cost=0.0,
        )
        self.qm.store.record_usage(big_usage)

        action, reason = self.qm.check_quota("heavy_user", estimated_tokens=1)
        assert action == QuotaAction.DOWNGRADE
        assert "downgrade" in reason

    def test_set_custom_limit(self):
        self.qm.set_limit(QuotaLevel.USER, QuotaLimit(max_concurrent_tasks=1))
        self.qm.store.start_task("u1")
        action, _ = self.qm.check_quota("u1")
        assert action == QuotaAction.QUEUE

    def test_department_isolation(self):
        """Different departments don't interfere"""
        for _ in range(5):
            self.qm.store.start_task("u1", "dept_a")
        self.qm.store.start_task("u2", "dept_b")

        # Dept A is over limit (6 concurrent)
        for _ in range(45):
            self.qm.store.start_task("ux", "dept_a")
        action_a, _ = self.qm.check_quota("u3", "dept_a")
        assert action_a == QuotaAction.QUEUE

        # Dept B is fine
        action_b, _ = self.qm.check_quota("u4", "dept_b")
        assert action_b == QuotaAction.ALLOW

    def test_usage_recording(self):
        record = UsageRecord(
            user_id="u1", department_id="d1",
            tokens_input=1000, tokens_output=500,
            cost=0.015, action="llm_call",
        )
        self.qm.store.record_usage(record)

        user_snap = self.qm.get_user_usage("u1")
        assert user_snap is not None
        assert user_snap.tokens_input == 1000
        assert user_snap.total_cost == 0.015

        dept_snap = self.qm.get_dept_usage("d1")
        assert dept_snap is not None
        assert dept_snap.tokens_input == 1000

    def test_system_level_limit(self):
        qm = QuotaManager()
        qm.set_limit(QuotaLevel.SYSTEM, QuotaLimit(max_daily_cost=1.0))
        big = UsageRecord(user_id="u1", cost=1.0, tokens_input=100, tokens_output=50)
        qm.store.record_usage(big)

        action, reason = qm.check_quota("u2", estimated_cost=0.01)
        assert action == QuotaAction.REJECT


class TestCostTracking:
    """Test cost tracking and attribution"""

    def test_estimate_cost(self):
        cost = estimate_cost("claude-sonnet-4-20250514", 1000, 500)
        assert cost > 0
        assert cost == pytest.approx((1000/1e6 * 3.0) + (500/1e6 * 15.0), rel=0.01)

        cost2 = estimate_cost("claude-haiku-4-5-20251001", 1000, 500)
        assert cost2 < cost  # Haiku is cheaper

    def test_record_call(self):
        tracker = CostTracker()
        record = tracker.record_call(
            agent_name="product_agent",
            task_id="task-1",
            model="claude-sonnet-4-20250514",
            input_tokens=1000,
            output_tokens=500,
            duration_ms=1500,
        )
        assert record.cost > 0
        assert record.agent_name == "product_agent"

    def test_task_cost_summary(self):
        tracker = CostTracker()
        tracker.record_call("agent_a", "t1", "claude-sonnet", 1000, 500)
        tracker.record_call("agent_a", "t1", "claude-sonnet", 2000, 1000)

        summary = tracker.get_task_summary("t1")
        assert summary is not None
        assert summary.llm_calls == 2
        assert summary.total_input_tokens == 3000

    def test_agent_cost_aggregation(self):
        tracker = CostTracker()
        tracker.record_call("agent_x", "t1", "claude-sonnet", 1000, 500)
        tracker.record_call("agent_x", "t2", "claude-sonnet", 2000, 1000)
        tracker.record_call("agent_y", "t3", "claude-sonnet", 500, 250)

        total_x = tracker.get_agent_costs("agent_x")
        total_y = tracker.get_agent_costs("agent_y")
        assert total_x > 0
        assert total_y > 0
        assert total_x > total_y  # agent_x used more tokens

    def test_anomaly_detection_expensive_call(self):
        tracker = CostTracker()
        tracker.record_call("agent_a", "t1", "claude-sonnet", 1000000, 500000)
        alerts = tracker.get_recent_alerts(min_severity="warning")
        assert any(a.alert_type == "cost_spike" for a in alerts)

    def test_anomaly_token_surge(self):
        tracker = CostTracker()
        tracker.record_call("agent_a", "t1", "claude-sonnet", 100000, 50)
        alerts = tracker.get_recent_alerts(min_severity="info")
        assert any(a.alert_type == "token_surge" for a in alerts)

    def test_cost_tracker_stats(self):
        tracker = CostTracker()
        tracker.record_call("agent_a", "t1", "claude-sonnet", 1000, 500)
        stats = tracker.get_stats()
        assert stats["total_tasks"] >= 1
        assert stats["total_cost"] > 0


class TestObservability:
    """Test metrics and tracing"""

    def setup_method(self):
        reset_graph()

    def test_metrics_with_empty_graph(self):
        calc = MetricsCalculator()
        metrics = calc.calculate_all()
        assert len(metrics) == 9
        # Empty graph should not crash
        assert metrics["end_to_end_success_rate"].value == 1.0

    def test_success_rate_with_data(self):
        g = get_graph()
        g.add_node(GraphNode(id="t1", type=NodeType.TASK, content={"status": "completed"}))
        g.add_node(GraphNode(id="t2", type=NodeType.TASK, content={"status": "completed"}))
        g.add_node(GraphNode(id="t3", type=NodeType.TASK, content={"status": "failed"}))

        calc = MetricsCalculator(g)
        rate = calc.end_to_end_success_rate()
        assert rate.value == pytest.approx(2/3, rel=0.01)
        assert rate.labels["completed"] == "2"
        assert rate.labels["total"] == "3"

    def test_avg_completion_time(self):
        from datetime import datetime, timezone, timedelta
        g = get_graph()
        now = datetime.now(timezone.utc)
        g.add_node(GraphNode(id="t1", type=NodeType.TASK, created_at=now - timedelta(seconds=100),
                             updated_at=now))
        g.add_node(GraphNode(id="t2", type=NodeType.TASK, created_at=now - timedelta(seconds=200),
                             updated_at=now))

        calc = MetricsCalculator(g)
        avg = calc.avg_completion_time()
        assert avg.value == pytest.approx(150, rel=10)  # ~150 seconds

    def test_failure_rate_by_agent(self):
        g = get_graph()
        g.add_node(GraphNode(id="t1", type=NodeType.TASK, content={"status": "completed", "agent": "a1"}))
        g.add_node(GraphNode(id="t2", type=NodeType.TASK, content={"status": "failed", "agent": "a1"}))
        g.add_node(GraphNode(id="t3", type=NodeType.TASK, content={"status": "completed", "agent": "a2"}))

        calc = MetricsCalculator(g)
        rate = calc.failure_rate_by_agent()
        assert rate.value == pytest.approx(1/3, rel=0.01)

    def test_reflection_trigger_rate(self):
        """Reflection rate = count(reflection decisions) / count(failures).
        Seed: 1 FAILURE node + 1 DECISION node of type 'reflection' => rate == 1.0.
        """
        g = get_graph()
        g.add_node(GraphNode(id="f1", type=NodeType.FAILURE, content={"error": "err1"}))
        g.add_node(GraphNode(
            id="r1", type=NodeType.DECISION,
            content={"type": "reflection", "summary": "agent retried after failure"},
        ))

        calc = MetricsCalculator(g)
        rate = calc.reflection_trigger_rate()
        assert rate.value == 1.0, f"expected 1.0 with 1 reflection + 1 failure, got {rate.value} ({rate.labels})"

    def test_experience_effectiveness(self):
        g = get_graph()
        g.add_node(GraphNode(id="e1", type=NodeType.EXPERIENCE, content={"success": True}))
        g.add_node(GraphNode(id="e2", type=NodeType.EXPERIENCE, content={"success": True}))
        g.add_node(GraphNode(id="e3", type=NodeType.EXPERIENCE, content={"success": False}))

        calc = MetricsCalculator(g)
        rate = calc.experience_effectiveness()
        assert rate.value == pytest.approx(2/3, rel=0.01)

    def test_simple_tracer(self):
        tracer = SimpleTracer()

        span = tracer.start_span("test_operation", trace_id="trace-test")
        time.sleep(0.01)
        tracer.end_span(span, status="ok")

        traces = tracer.get_trace("trace-test")
        assert len(traces) == 1
        assert traces[0].name == "test_operation"
        assert traces[0].duration_ms > 0

    def test_tracer_stats(self):
        tracer = SimpleTracer()
        s1 = tracer.start_span("op1")
        tracer.end_span(s1)
        s2 = tracer.start_span("op2")
        tracer.end_span(s2, status="error")

        stats = tracer.get_stats()
        assert stats["total_spans"] == 2
        assert stats["error_count"] == 1

    def test_tracer_parent_child(self):
        tracer = SimpleTracer()
        parent = tracer.start_span("parent", trace_id="trace-pc")
        child = tracer.start_span("child", trace_id="trace-pc", parent_id=parent.span_id)
        tracer.end_span(child)
        tracer.end_span(parent)

        trace = tracer.get_trace("trace-pc")
        assert len(trace) == 2
        assert trace[0].parent_id == parent.span_id

    def test_prometheus_export(self):
        reset_graph()
        g = get_graph()
        g.add_node(GraphNode(id="pt1", type=NodeType.TASK, content={"status": "completed"}))

        tracer = SimpleTracer()
        text = tracer.to_prometheus_text()
        assert "# HELP" in text
        assert "# TYPE" in text
        assert "agent_end_to_end_success_rate" in text

    def test_all_9_metrics_exist(self):
        calc = MetricsCalculator()
        metrics = calc.calculate_all()
        expected = [
            "end_to_end_success_rate",
            "avg_completion_time",
            "cost_per_task",
            "user_satisfaction",
            "failure_rate_by_agent",
            "reflection_trigger_rate",
            "escalation_request_rate",
            "validation_failure_rate",
            "experience_effectiveness",
        ]
        for name in expected:
            assert name in metrics, f"Missing metric: {name}"


class TestQuotaQueue:
    """Real FIFO waiting queue tests"""

    def test_queue_enqueues_when_full(self):
        qm = QuotaManager()
        # Fill up the concurrent slots
        for _ in range(5):
            qm.check_quota("busy_user")  # ALLOW
            qm.store.start_task("busy_user")

        # Now action should QUEUE
        action, _ = qm.check_quota("busy_user")
        assert action == QuotaAction.QUEUE

        # Enqueue should succeed (under max_queued)
        assert qm.enqueue("busy_user") is True
        assert qm.queue_size("busy_user") == 1

    def test_queue_rejects_when_at_max(self):
        qm = QuotaManager()
        # max_queued defaults to 10 for USER
        for _ in range(10):
            qm.enqueue("max_user")
        # 11th should fail
        assert qm.enqueue("max_user") is False
        assert qm.queue_size("max_user") == 10

    def test_dequeue_frees_slot(self):
        qm = QuotaManager()
        qm.enqueue("queued_user")
        assert qm.queue_size("queued_user") == 1
        # Pop one
        popped = qm.dequeue("queued_user")
        assert popped is True
        assert qm.queue_size("queued_user") == 0

    def test_dequeue_empty_returns_false(self):
        qm = QuotaManager()
        assert qm.dequeue("nonexistent") is False

    def test_notify_next_wakes_one(self):
        qm = QuotaManager()
        qm.enqueue("wake_user")
        qm.enqueue("wake_user")
        assert qm.queue_size("wake_user") == 2

        # Slot freed -> notify_next pops one
        result = qm.notify_next("wake_user")
        assert result is True
        assert qm.queue_size("wake_user") == 1

    def test_dept_queue_isolation(self):
        qm = QuotaManager()
        qm.enqueue("u1", "dept_a")
        qm.enqueue("u2", "dept_b")
        assert qm.queue_size("u1", "dept_a") == 1
        assert qm.queue_size("u2", "dept_b") == 1


class TestCostTrackerDedup:
    """Cost tracker sliding-window dedup"""

    def test_dedup_suppresses_repeats(self):
        from agent_system.core.cost_tracker import CostTracker
        tracker = CostTracker()

        # Trigger same alert 3 times in quick succession
        for _ in range(3):
            tracker.record_call("agent_a", "task-1", "claude-sonnet", 1_000_000, 500_000)

        # Without dedup: 3 alerts of cost_spike
        # With dedup: only 1 (the others suppressed in 5-min window)
        spike_alerts = [a for a in tracker._anomalies if a.alert_type == "cost_spike"
                       and "Expensive" in a.message]
        assert len(spike_alerts) == 1

    def test_different_sources_not_deduped(self):
        from agent_system.core.cost_tracker import CostTracker
        tracker = CostTracker()

        # Two different agents trigger expensive calls
        tracker.record_call("agent_a", "task-1", "claude-sonnet", 1_000_000, 500_000)
        tracker.record_call("agent_b", "task-2", "claude-sonnet", 1_000_000, 500_000)

        spike_alerts = [a for a in tracker._anomalies if a.alert_type == "cost_spike"
                       and "Expensive" in a.message]
        assert len(spike_alerts) == 2  # different agents, no dedup
