"""
Observability — metrics, tracing, and Dataview-style queries

ARCHITECTURE.md Ch.10 / L6:
  9 auto-calculated metrics from MultiLinkGraph
  OpenTelemetry tracing skeleton
  Prometheus-compatible metrics endpoint

9 Metrics:
  1. end_to_end_success_rate
  2. avg_completion_time
  3. cost_per_task
  4. user_satisfaction
  5. failure_rate_by_agent
  6. reflection_trigger_rate
  7. escalation_request_rate
  8. validation_failure_rate
  9. experience_effectiveness
"""

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from agent_system.memory.graph import (
    MultiLinkGraph,
    get_graph,
    NodeType,
    LinkType,
)

from agent_system.core.dataview import query as dataview_query

logger = logging.getLogger(__name__)


class MetricValue(BaseModel):
    """A single metric value with metadata"""
    name: str
    value: float
    unit: str = "count"
    labels: Dict[str, str] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MetricsSnapshot(BaseModel):
    """Snapshot of all 9 metrics"""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metrics: Dict[str, MetricValue] = Field(default_factory=dict)


class TraceSpan(BaseModel):
    """A single span in a trace"""
    span_id: str = ""
    trace_id: str = ""
    parent_id: Optional[str] = None
    name: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: float = 0.0
    status: str = "ok"  # ok / error
    attributes: Dict[str, Any] = Field(default_factory=dict)


class MetricsCalculator:
    """
    Calculates the 9 key metrics from the MultiLinkGraph.

    PR 1 (P0-1): Migrated to use the Dataview engine (PR 1 deliverable).
    SQL queries express the metric definitions; ratios computed in Python
    layer where the Dataview engine doesn't yet support binary expressions.

    All metrics are calculated at query time (no caching) as
    specified in ARCHITECTURE.md Ch.10.3.
    """

    def __init__(self, graph: Optional[MultiLinkGraph] = None):
        self.graph = graph or get_graph()

    def _safe_query(self, sql: str) -> Dict[str, float]:
        """Run a dataview query and return its aggregations. Empty dict on parse failure."""
        try:
            return dataview_query(sql, graph=self.graph).aggregations
        except Exception as e:
            logger.debug(f"Dataview query failed: {e}\nSQL: {sql}")
            return {}

    def _safe_count(self, sql: str) -> int:
        """Run a count query and return the integer value."""
        return int(self._safe_query(sql).get("count", 0) or 0)

    def calculate_all(self) -> Dict[str, MetricValue]:
        """Calculate all 9 metrics at once"""
        return {
            "end_to_end_success_rate": self.end_to_end_success_rate(),
            "avg_completion_time": self.avg_completion_time(),
            "cost_per_task": self.cost_per_task(),
            "user_satisfaction": self.user_satisfaction(),
            "failure_rate_by_agent": self.failure_rate_by_agent(),
            "reflection_trigger_rate": self.reflection_trigger_rate(),
            "escalation_request_rate": self.escalation_request_rate(),
            "validation_failure_rate": self.validation_failure_rate(),
            "experience_effectiveness": self.experience_effectiveness(),
        }

    def end_to_end_success_rate(self) -> MetricValue:
        """Metric 1: COUNT(completed) / COUNT(total) via Dataview"""
        completed = self._safe_count(
            "SELECT COUNT(*) AS count FROM tasks WHERE status = 'completed';"
        )
        total = self._safe_count("SELECT COUNT(*) AS count FROM tasks;")
        rate = completed / total if total > 0 else 1.0
        return MetricValue(
            name="end_to_end_success_rate",
            value=round(rate, 4),
            unit="ratio",
            labels={"total": str(total), "completed": str(completed)},
        )

    def avg_completion_time(self) -> MetricValue:
        """Metric 2: AVG(duration_seconds) via Dataview"""
        aggs = self._safe_query(
            "SELECT AVG(content.duration_seconds) AS avg FROM tasks;"
        )
        avg = aggs.get("avg", 0.0)
        return MetricValue(
            name="avg_completion_time",
            value=round(avg, 2),
            unit="seconds",
            labels={"source": "dataview"},
        )

    def cost_per_task(self) -> MetricValue:
        """Metric 3: SUM(cost) / COUNT(tasks) via Dataview"""
        sum_aggs = self._safe_query("SELECT SUM(content.cost) AS sum FROM tasks;")
        total = self._safe_count("SELECT COUNT(*) AS count FROM tasks;")
        total_cost = sum_aggs.get("sum", 0.0)
        avg_cost = total_cost / total if total > 0 else 0.0
        return MetricValue(
            name="cost_per_task",
            value=round(avg_cost, 4),
            unit="usd",
            labels={"total_cost": str(round(total_cost, 4))},
        )

    def user_satisfaction(self) -> MetricValue:
        """Metric 4: AVG(feedback.score) via Dataview"""
        aggs = self._safe_query("SELECT AVG(content.score) AS avg FROM feedbacks;")
        avg = aggs.get("avg", 0.0)
        return MetricValue(
            name="user_satisfaction",
            value=round(avg, 2),
            unit="score",
            labels={"source": "dataview"},
        )

    def failure_rate_by_agent(self) -> MetricValue:
        """Metric 5: COUNT(failures) / COUNT(tasks). Note: per-agent breakdown
        requires GROUP BY which is not in PR 1 scope — overall rate returned."""
        failed = self._safe_count("SELECT COUNT(*) AS count FROM tasks WHERE status = 'failed';")
        total = self._safe_count("SELECT COUNT(*) AS count FROM tasks;")
        rate = failed / total if total > 0 else 0.0
        return MetricValue(
            name="failure_rate_by_agent",
            value=round(rate, 4),
            unit="ratio",
            labels={"failed": str(failed), "total": str(total), "note": "per-agent breakdown deferred to PR2+"},
        )

    def reflection_trigger_rate(self) -> MetricValue:
        """Metric 6: COUNT(reflections)/COUNT(failures). Reflections are stored
        as DECISION nodes that link from FAILURE nodes."""
        reflections = self._safe_count("SELECT COUNT(*) AS count FROM decisions WHERE content.type = 'reflection';")
        failures = self._safe_count("SELECT COUNT(*) AS count FROM failures;")
        rate = reflections / failures if failures > 0 else 0.0
        return MetricValue(
            name="reflection_trigger_rate",
            value=round(rate, 4),
            unit="ratio",
            labels={"reflections": str(reflections), "failures": str(failures)},
        )

    def escalation_request_rate(self) -> MetricValue:
        """Metric 7: COUNT(escalations)/COUNT(failures). Escalations recorded
        as DECISION nodes with type='escalation'."""
        escalations = self._safe_count("SELECT COUNT(*) AS count FROM decisions WHERE content.type = 'escalation';")
        failures = self._safe_count("SELECT COUNT(*) AS count FROM failures;")
        rate = escalations / failures if failures > 0 else 0.0
        return MetricValue(
            name="escalation_request_rate",
            value=round(rate, 4),
            unit="ratio",
            labels={"escalations": str(escalations), "failures": str(failures)},
        )

    def validation_failure_rate(self) -> MetricValue:
        """Metric 8: COUNT(validation_fails)/COUNT(outputs). Validation fails
        recorded as OUTPUT nodes with content.valid=false."""
        invalid = self._safe_count("SELECT COUNT(*) AS count FROM outputs WHERE content.valid = false;")
        total = self._safe_count("SELECT COUNT(*) AS count FROM outputs;")
        rate = invalid / total if total > 0 else 0.0
        return MetricValue(
            name="validation_failure_rate",
            value=round(rate, 4),
            unit="ratio",
            labels={"invalid": str(invalid), "total": str(total)},
        )

    def experience_effectiveness(self) -> MetricValue:
        """Metric 9: AVG(success_rate) for EXPERIENCE nodes via Dataview"""
        successes = self._safe_count("SELECT COUNT(*) AS count FROM experiences WHERE content.success = true;")
        total = self._safe_count("SELECT COUNT(*) AS count FROM experiences;")
        rate = successes / total if total > 0 else 0.0
        return MetricValue(
            name="experience_effectiveness",
            value=round(rate, 4),
            unit="ratio",
            labels={"successful": str(successes), "total": str(total)},
        )

    # ── Legacy methods (preserved for rollback reference) ──
    # These are the original hand-calculated implementations from PR-pre-1.
    # They are NOT called by calculate_all() anymore but kept for reference
    # and for safety in case the Dataview migration needs to be rolled back.

    def _legacy_end_to_end_success_rate(self) -> float:
        tasks = self.graph.find_nodes(node_type=NodeType.TASK)
        total = len(tasks)
        if total == 0:
            return 1.0
        completed = sum(1 for t in tasks if t.content.get("status") == "completed")
        return completed / total if total > 0 else 1.0

    def _legacy_avg_completion_time(self) -> float:
        tasks = self.graph.find_nodes(node_type=NodeType.TASK)
        durations = []
        for t in tasks:
            created = t.created_at
            updated = t.updated_at
            if created and updated:
                try:
                    dur = (updated - created).total_seconds()
                    if dur > 0:
                        durations.append(dur)
                except Exception:
                    continue
        return sum(durations) / len(durations) if durations else 0.0


# ── Simple Tracer ──

class SimpleTracer:
    """
    Lightweight distributed tracing.

    Usage:
        tracer = SimpleTracer()
        span = tracer.start_span("agent_execute", trace_id="trace-1")
        # ... do work ...
        tracer.end_span(span)
    """

    def __init__(self):
        self._spans: List[TraceSpan] = []
        self._max_spans: int = 10000

    def start_span(
        self,
        name: str,
        trace_id: str = "",
        parent_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> TraceSpan:
        import uuid
        span = TraceSpan(
            span_id=f"span-{uuid.uuid4().hex[:8]}",
            trace_id=trace_id or f"trace-{uuid.uuid4().hex[:8]}",
            parent_id=parent_id,
            name=name,
            start_time=time.time(),
            attributes=attributes or {},
        )
        return span

    def end_span(self, span: TraceSpan, status: str = "ok"):
        span.end_time = time.time()
        span.duration_ms = round((span.end_time - span.start_time) * 1000, 2)
        span.status = status
        self._spans.append(span)

        if len(self._spans) > self._max_spans:
            self._spans = self._spans[-self._max_spans:]

    def get_trace(self, trace_id: str) -> List[TraceSpan]:
        return [s for s in self._spans if s.trace_id == trace_id]

    def get_recent_spans(self, limit: int = 100) -> List[TraceSpan]:
        return self._spans[-limit:]

    def get_stats(self) -> Dict[str, Any]:
        if not self._spans:
            return {"total_spans": 0}
        durations = [s.duration_ms for s in self._spans if s.duration_ms > 0]
        return {
            "total_spans": len(self._spans),
            "avg_duration_ms": round(sum(durations) / len(durations), 2) if durations else 0,
            "error_count": sum(1 for s in self._spans if s.status == "error"),
            "span_names": list(set(s.name for s in self._spans)),
        }

    def to_prometheus_text(self) -> str:
        """Export metrics in Prometheus text format"""
        lines = ["# HELP agent_system_metrics Agent System metrics",
                 "# TYPE agent_system_metrics gauge"]
        calc = MetricsCalculator()
        metrics = calc.calculate_all()
        for name, metric in metrics.items():
            labels = ",".join(f'{k}="{v}"' for k, v in metric.labels.items())
            if labels:
                lines.append(f'agent_{name}{{{labels}}} {metric.value}')
            else:
                lines.append(f'agent_{name} {metric.value}')
        return "\n".join(lines)


# Global instances
metrics_calculator = MetricsCalculator()
tracer = SimpleTracer()
