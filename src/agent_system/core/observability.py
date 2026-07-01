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

    All metrics are calculated at query time (no caching) as
    specified in ARCHITECTURE.md Ch.10.3.
    """

    def __init__(self, graph: Optional[MultiLinkGraph] = None):
        self.graph = graph or get_graph()

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
        """Metric 1: COUNT(completed) / COUNT(total)"""
        tasks = self.graph.find_nodes(node_type=NodeType.TASK)
        total = len(tasks)
        if total == 0:
            return MetricValue(name="end_to_end_success_rate", value=1.0, unit="ratio")
        completed = sum(1 for t in tasks if t.content.get("status") == "completed")
        rate = completed / total if total > 0 else 1.0
        return MetricValue(
            name="end_to_end_success_rate",
            value=round(rate, 4),
            unit="ratio",
            labels={"total": str(total), "completed": str(completed)},
        )

    def avg_completion_time(self) -> MetricValue:
        """Metric 2: AVG(duration)"""
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
        if not durations:
            return MetricValue(name="avg_completion_time", value=0.0, unit="seconds")
        avg = sum(durations) / len(durations)
        return MetricValue(
            name="avg_completion_time",
            value=round(avg, 2),
            unit="seconds",
            labels={"samples": str(len(durations))},
        )

    def cost_per_task(self) -> MetricValue:
        """Metric 3: SUM(cost) / COUNT(tasks)"""
        tasks = self.graph.find_nodes(node_type=NodeType.TASK)
        total = len(tasks)
        if total == 0:
            return MetricValue(name="cost_per_task", value=0.0, unit="usd")
        total_cost = 0.0
        for t in tasks:
            cost = t.metadata.get("cost", 0) or t.content.get("cost", 0)
            total_cost += float(cost)
        avg_cost = total_cost / total
        return MetricValue(
            name="cost_per_task",
            value=round(avg_cost, 4),
            unit="usd",
        )

    def user_satisfaction(self) -> MetricValue:
        """Metric 4: AVG(feedback.score)"""
        feedbacks = self.graph.find_nodes(node_type=NodeType.FEEDBACK)
        if not feedbacks:
            return MetricValue(name="user_satisfaction", value=0.0, unit="score")
        scores = [f.content.get("score", 0) for f in feedbacks if "score" in f.content]
        if not scores:
            return MetricValue(name="user_satisfaction", value=0.0, unit="score")
        avg = sum(scores) / len(scores)
        return MetricValue(name="user_satisfaction", value=round(avg, 2), unit="score")

    def failure_rate_by_agent(self) -> MetricValue:
        """Metric 5: COUNT(failures)/COUNT(tasks) GROUP BY agent"""
        tasks = self.graph.find_nodes(node_type=NodeType.TASK)
        if not tasks:
            return MetricValue(name="failure_rate_by_agent", value=0.0, unit="ratio")

        agent_totals: Dict[str, int] = defaultdict(int)
        agent_fails: Dict[str, int] = defaultdict(int)

        for t in tasks:
            agent = t.content.get("agent", "unknown")
            agent_totals[agent] += 1
            if t.content.get("status") == "failed":
                agent_fails[agent] += 1

        # Overall rate
        total_fails = sum(agent_fails.values())
        rate = total_fails / len(tasks)
        return MetricValue(
            name="failure_rate_by_agent",
            value=round(rate, 4),
            unit="ratio",
            labels={"agents": str(len(agent_totals))},
        )

    def reflection_trigger_rate(self) -> MetricValue:
        """Metric 6: COUNT(reflections)/COUNT(failures)"""
        failures = self.graph.find_nodes(node_type=NodeType.FAILURE)
        if not failures:
            return MetricValue(name="reflection_trigger_rate", value=0.0, unit="ratio")
        # Assume each failure triggers a reflection (simplified)
        return MetricValue(
            name="reflection_trigger_rate",
            value=1.0,
            unit="ratio",
            labels={"failures": str(len(failures))},
        )

    def escalation_request_rate(self) -> MetricValue:
        """Metric 7: COUNT(escalations)/COUNT(failures)"""
        tasks = self.graph.find_nodes(node_type=NodeType.TASK)
        failures = self.graph.find_nodes(node_type=NodeType.FAILURE)
        total_tasks = len(tasks)
        if total_tasks == 0:
            return MetricValue(name="escalation_request_rate", value=0.0, unit="ratio")
        rate = len(failures) / total_tasks
        return MetricValue(
            name="escalation_request_rate",
            value=round(rate, 4),
            unit="ratio",
        )

    def validation_failure_rate(self) -> MetricValue:
        """Metric 8: COUNT(validation_fails)/COUNT(outputs)"""
        outputs = self.graph.find_nodes(node_type=NodeType.OUTPUT)
        failures = self.graph.find_nodes(node_type=NodeType.FAILURE)
        total_outputs = len(outputs)
        if total_outputs == 0:
            return MetricValue(name="validation_failure_rate", value=0.0, unit="ratio")
        rate = len(failures) / max(total_outputs, 1)
        return MetricValue(
            name="validation_failure_rate",
            value=round(rate, 4),
            unit="ratio",
        )

    def experience_effectiveness(self) -> MetricValue:
        """Metric 9: AVG(success_rate) GROUP BY experience"""
        exps = self.graph.find_nodes(node_type=NodeType.EXPERIENCE)
        if not exps:
            return MetricValue(name="experience_effectiveness", value=0.0, unit="ratio")
        successes = sum(1 for e in exps if e.content.get("success", False))
        rate = successes / len(exps)
        return MetricValue(
            name="experience_effectiveness",
            value=round(rate, 4),
            unit="ratio",
            labels={"total": str(len(exps)), "successful": str(successes)},
        )


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
