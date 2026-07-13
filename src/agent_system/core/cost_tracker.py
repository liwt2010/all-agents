"""
Cost Tracking — per-task LLM usage + attribution + anomaly detection

Tracks:
  - Per-task LLM calls (input/output tokens, model, cost)
  - Cost attribution by agent/user/department
  - Anomaly alerts for cost spikes
"""

import logging
import time
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from agent_system.core.quota import UsageRecord, QuotaManager, quota_manager

logger = logging.getLogger(__name__)

# Cost per 1M tokens (approximate)
MODEL_PRICING = {
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-sonnet": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
    "claude-haiku": {"input": 0.25, "output": 1.25},
    "default": {"input": 3.0, "output": 15.0},
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for an LLM call"""
    pricing = MODEL_PRICING.get("default")
    for key, p in MODEL_PRICING.items():
        if key in model:
            pricing = p
            break
    return (input_tokens / 1_000_000 * pricing["input"] +
            output_tokens / 1_000_000 * pricing["output"])


class LLMCallRecord(BaseModel):
    """Record of a single LLM API call"""
    timestamp: float = Field(default_factory=time.time)
    agent_name: str = ""
    task_id: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    duration_ms: float = 0.0
    success: bool = True


class TaskCostSummary(BaseModel):
    """Cost summary for a single task"""
    task_id: str = ""
    agent_name: str = ""
    user_id: str = ""
    department_id: str = ""
    llm_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost: float = 0.0
    duration_seconds: float = 0.0
    started_at: datetime | None = None
    completed_at: datetime | None = None


class AnomalyAlert(BaseModel):
    """Alert for anomalous cost patterns"""
    alert_type: str  # cost_spike / token_surge / unusual_model
    severity: str = "warning"  # info / warning / critical
    message: str = ""
    value: float = 0.0
    threshold: float = 0.0
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CostTracker:
    """
    Tracks LLM costs with attribution and anomaly detection.

    Integrates with QuotaManager for enforcement.
    """

    def __init__(self, quota_mgr: QuotaManager | None = None):
        self.quota_mgr = quota_mgr or quota_manager
        self._task_costs: dict[str, TaskCostSummary] = {}
        self._recent_calls: list[LLMCallRecord] = []
        self._anomalies: list[AnomalyAlert] = []
        self._max_recent_calls: int = 1000

    def record_call(
        self,
        agent_name: str,
        task_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: float = 0.0,
        success: bool = True,
        user_id: str = "",
        department_id: str = "",
    ) -> LLMCallRecord:
        """Record an LLM API call and update quota usage"""
        cost = estimate_cost(model, input_tokens, output_tokens)

        record = LLMCallRecord(
            agent_name=agent_name,
            task_id=task_id,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost=cost,
            duration_ms=duration_ms,
            success=success,
        )

        # Update task summary
        if task_id not in self._task_costs:
            self._task_costs[task_id] = TaskCostSummary(
                task_id=task_id, agent_name=agent_name,
                user_id=user_id, department_id=department_id,
            )
        summary = self._task_costs[task_id]
        summary.llm_calls += 1
        summary.total_input_tokens += input_tokens
        summary.total_output_tokens += output_tokens
        summary.total_cost += cost

        # Report to quota manager
        usage_record = UsageRecord(
            user_id=user_id or agent_name,
            department_id=department_id,
            agent_name=agent_name,
            task_id=task_id,
            tokens_input=input_tokens,
            tokens_output=output_tokens,
            cost=cost,
            action="llm_call",
        )
        self.quota_mgr.record_usage(usage_record)

        # Keep recent calls buffer
        self._recent_calls.append(record)
        if len(self._recent_calls) > self._max_recent_calls:
            self._recent_calls = self._recent_calls[-self._max_recent_calls:]

        # Anomaly detection
        self._check_anomalies(record, summary)

        return record

    def get_task_summary(self, task_id: str) -> TaskCostSummary | None:
        return self._task_costs.get(task_id)

    def get_agent_costs(self, agent_name: str) -> float:
        """Total cost for an agent across all tasks"""
        return sum(
            s.total_cost for s in self._task_costs.values()
            if s.agent_name == agent_name
        )

    def get_total_costs(self) -> float:
        return sum(s.total_cost for s in self._task_costs.values())

    def get_recent_alerts(self, min_severity: str = "warning") -> list[AnomalyAlert]:
        return [a for a in self._anomalies
                if {"info": 0, "warning": 1, "critical": 2}.get(a.severity, 0) >=
                   {"info": 0, "warning": 1, "critical": 2}.get(min_severity, 0)]

    def _should_suppress(self, alert: AnomalyAlert, dedup_window_seconds: float = 300.0) -> bool:
        """Sliding-window dedup: suppress duplicate alert_type+source within window."""
        from datetime import timedelta
        cutoff = alert.timestamp - timedelta(seconds=dedup_window_seconds)
        source_key = alert.message.split(" by ")[-1] if " by " in alert.message else ""
        for existing in self._anomalies:
            if (existing.alert_type == alert.alert_type
                and existing.message.split(" by ")[-1] == source_key
                and existing.timestamp > cutoff
                and existing.severity == alert.severity):
                return True
        return False

    def _check_anomalies(self, call: LLMCallRecord, summary: TaskCostSummary):
        """Check for anomalous cost patterns. Uses sliding-window dedup."""
        # Single call too expensive
        if call.cost > 1.0:
            alert = AnomalyAlert(
                alert_type="cost_spike",
                severity="warning",
                message=f"Expensive LLM call: ${call.cost:.4f} by {call.agent_name}",
                value=call.cost,
                threshold=1.0,
            )
            if not self._should_suppress(alert):
                self._anomalies.append(alert)

        # Task accumulating high cost
        if summary.total_cost > 5.0:
            alert = AnomalyAlert(
                alert_type="cost_spike",
                severity="warning",
                message=f"Task {summary.task_id} cost ${summary.total_cost:.4f}",
                value=summary.total_cost,
                threshold=5.0,
            )
            if not self._should_suppress(alert):
                self._anomalies.append(alert)

        # Token surge
        if call.input_tokens > 50000:
            alert = AnomalyAlert(
                alert_type="token_surge",
                severity="info",
                message=f"Large input: {call.input_tokens} tokens by {call.agent_name}",
                value=call.input_tokens,
                threshold=50000,
            )
            if not self._should_suppress(alert):
                self._anomalies.append(alert)

    def get_stats(self) -> dict[str, Any]:
        """Get overall cost statistics"""
        total = self.get_total_costs()
        call_count = len(self._recent_calls)
        return {
            "total_cost": round(total, 4),
            "total_calls": call_count,
            "total_tasks": len(self._task_costs),
            "recent_alerts": len(self._anomalies),
            "agents": list(set(s.agent_name for s in self._task_costs.values())),
        }


# Global cost tracker
cost_tracker = CostTracker()
