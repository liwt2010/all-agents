"""
Resource Quota — 4-level quota system (ARCHITECTURE.md 4.5)

Levels:
  - User       : 5 concurrent tasks / $5 daily / 1M token
  - Department : 50 concurrent tasks / $200 daily / 50M token
  - System     : 200 concurrent tasks / $2000 daily / 500M token
  - LLM API    : 100 token/sec / 1000 queued

Actions on over-quota: queue / downgrade / reject
"""

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field


class QuotaLevel(str, Enum):
    USER = "user"
    DEPARTMENT = "department"
    SYSTEM = "system"
    LLM_API = "llm_api"


class QuotaAction(str, Enum):
    ALLOW = "allow"
    QUEUE = "queue"
    DOWNGRADE = "downgrade"
    REJECT = "reject"


class QuotaLimit(BaseModel):
    """Definition of a quota limit"""
    max_concurrent_tasks: int = 5
    max_daily_cost: float = 5.0      # USD
    max_daily_tokens: int = 1_000_000
    max_daily_requests: int = 1000
    max_queued: int = 10


# Default quota limits per level
DEFAULT_QUOTAS = {
    QuotaLevel.USER: QuotaLimit(
        max_concurrent_tasks=5,
        max_daily_cost=5.0,
        max_daily_tokens=1_000_000,
        max_daily_requests=1000,
        max_queued=10,
    ),
    QuotaLevel.DEPARTMENT: QuotaLimit(
        max_concurrent_tasks=50,
        max_daily_cost=200.0,
        max_daily_tokens=50_000_000,
        max_daily_requests=10000,
        max_queued=50,
    ),
    QuotaLevel.SYSTEM: QuotaLimit(
        max_concurrent_tasks=200,
        max_daily_cost=2000.0,
        max_daily_tokens=500_000_000,
        max_daily_requests=50000,
        max_queued=200,
    ),
    QuotaLevel.LLM_API: QuotaLimit(
        max_concurrent_tasks=1000,
        max_daily_cost=10000.0,
        max_daily_tokens=10_000_000_000,
        max_daily_requests=100000,
        max_queued=1000,
    ),
}


class UsageRecord(BaseModel):
    """A single usage record"""
    timestamp: float = Field(default_factory=time.time)
    user_id: str = ""
    department_id: str = ""
    agent_name: str = ""
    task_id: str = ""
    tokens_input: int = 0
    tokens_output: int = 0
    cost: float = 0.0
    action: str = ""  # llm_call / task_run / tool_call


class UsageSnapshot(BaseModel):
    """Usage snapshot for a given period"""
    date_str: str = ""  # YYYY-MM-DD
    user_id: str = ""
    department_id: str = ""
    total_tasks: int = 0
    concurrent_tasks: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    total_cost: float = 0.0
    total_requests: int = 0


class QuotaStore:
    """In-memory quota state with daily reset"""

    def __init__(self, initial_llm_rate: float = 100.0):
        self._today = date.today()
        self._user_usage: dict[str, UsageSnapshot] = {}
        self._dept_usage: dict[str, UsageSnapshot] = {}
        self._system_usage = UsageSnapshot(date_str=str(self._today))
        self._concurrent: dict[str, int] = defaultdict(int)  # user_id -> count
        self._dept_concurrent: dict[str, int] = defaultdict(int)
        self._llm_token_bucket: float = initial_llm_rate  # start full
        self._llm_last_refill: float = time.time()
        # Real FIFO waiting queues per scope (user_id or dept_id)
        self._user_queues: dict[str, "asyncio.Queue"] = {}
        self._dept_queues: dict[str, "asyncio.Queue"] = {}
        self._queued_user_count: dict[str, int] = {}
        self._queued_dept_count: dict[str, int] = {}

    def _check_reset(self):
        """Reset daily counters if day changed"""
        today = date.today()
        if today != self._today:
            self._today = today
            self._user_usage.clear()
            self._dept_usage.clear()
            self._system_usage = UsageSnapshot(date_str=str(today))

    def _get_user_snapshot(self, user_id: str) -> UsageSnapshot:
        self._check_reset()
        if user_id not in self._user_usage:
            self._user_usage[user_id] = UsageSnapshot(date_str=str(self._today), user_id=user_id)
        return self._user_usage[user_id]

    def _get_dept_snapshot(self, dept_id: str) -> UsageSnapshot:
        self._check_reset()
        if dept_id not in self._dept_usage:
            self._dept_usage[dept_id] = UsageSnapshot(date_str=str(self._today), department_id=dept_id)
        return self._dept_usage[dept_id]

    def record_usage(self, record: UsageRecord):
        """Record usage and update all counter levels"""
        self._check_reset()

        # User level
        user_snap = self._get_user_snapshot(record.user_id)
        user_snap.tokens_input += record.tokens_input
        user_snap.tokens_output += record.tokens_output
        user_snap.total_cost += record.cost
        user_snap.total_requests += 1

        # Department level
        if record.department_id:
            dept_snap = self._get_dept_snapshot(record.department_id)
            dept_snap.tokens_input += record.tokens_input
            dept_snap.tokens_output += record.tokens_output
            dept_snap.total_cost += record.cost
            dept_snap.total_requests += 1

        # System level
        self._system_usage.tokens_input += record.tokens_input
        self._system_usage.tokens_output += record.tokens_output
        self._system_usage.total_cost += record.cost
        self._system_usage.total_requests += 1

    def start_task(self, user_id: str, department_id: str = ""):
        self._concurrent[user_id] += 1
        if department_id:
            self._dept_concurrent[department_id] += 1

    def end_task(self, user_id: str, department_id: str = ""):
        self._concurrent[user_id] = max(0, self._concurrent[user_id] - 1)
        if department_id:
            self._dept_concurrent[department_id] = max(0, self._dept_concurrent[department_id] - 1)

    def get_llm_available_tokens(self, max_rate: float = 100.0) -> float:
        """Token bucket for LLM API rate limiting"""
        now = time.time()
        elapsed = now - self._llm_last_refill
        self._llm_token_bucket = min(max_rate, self._llm_token_bucket + elapsed * max_rate)
        self._llm_last_refill = now
        return self._llm_token_bucket


class QuotaManager:
    """
    4-level quota enforcer.

    Usage:
        qm = QuotaManager()
        result = qm.check_quota("user_1", "dept_a")
        if result.action == QuotaAction.ALLOW:
            qm.store.start_task("user_1", "dept_a")
            # ... do work ...
            qm.store.end_task("user_1", "dept_a")
    """

    def __init__(self):
        self.store = QuotaStore()
        self.limits: dict[QuotaLevel, QuotaLimit] = DEFAULT_QUOTAS.copy()

    def set_limit(self, level: QuotaLevel, limit: QuotaLimit):
        """Override default quota for a level"""
        self.limits[level] = limit

    def check_quota(
        self,
        user_id: str,
        department_id: str = "",
        estimated_cost: float = 0.0,
        estimated_tokens: int = 0,
    ) -> tuple[QuotaAction, str]:
        """
        Check if a task should be allowed at all levels.

        Returns (action, reason).
        """
        limits = self.limits

        # 1. User-level checks
        user_snap = self.store._get_user_snapshot(user_id)
        user_concurrent = self.store._concurrent.get(user_id, 0)

        if user_concurrent >= limits[QuotaLevel.USER].max_concurrent_tasks:
            return (QuotaAction.QUEUE, f"User {user_id}: too many concurrent tasks ({user_concurrent})")
        if user_snap.total_cost + estimated_cost > limits[QuotaLevel.USER].max_daily_cost:
            return (QuotaAction.REJECT, f"User {user_id}: daily cost limit ${limits[QuotaLevel.USER].max_daily_cost}")
        if user_snap.tokens_input + estimated_tokens > limits[QuotaLevel.USER].max_daily_tokens:
            return (QuotaAction.DOWNGRADE, f"User {user_id}: daily token limit exceeded, will downgrade model")

        # 2. Department-level checks
        if department_id:
            dept_snap = self.store._get_dept_snapshot(department_id)
            dept_concurrent = self.store._dept_concurrent.get(department_id, 0)

            if dept_concurrent >= limits[QuotaLevel.DEPARTMENT].max_concurrent_tasks:
                return (QuotaAction.QUEUE, f"Dept {department_id}: too many concurrent tasks ({dept_concurrent})")
            if dept_snap.total_cost + estimated_cost > limits[QuotaLevel.DEPARTMENT].max_daily_cost:
                return (QuotaAction.REJECT, f"Dept {department_id}: daily cost limit ${limits[QuotaLevel.DEPARTMENT].max_daily_cost}")

        # 3. System-level checks
        sys_snap = self.store._system_usage
        if sys_snap.total_cost + estimated_cost > limits[QuotaLevel.SYSTEM].max_daily_cost:
            return (QuotaAction.REJECT, "System: daily cost limit reached")
        if sys_snap.tokens_input + estimated_tokens > limits[QuotaLevel.SYSTEM].max_daily_tokens:
            return (QuotaAction.DOWNGRADE, "System: daily token limit exceeded")

        # 4. LLM API rate check
        available = self.store.get_llm_available_tokens(limits[QuotaLevel.LLM_API].max_concurrent_tasks)
        if available < 1:
            return (QuotaAction.QUEUE, "LLM API: rate limit reached, queuing request")

        return (QuotaAction.ALLOW, "")

    def record_usage(self, record: UsageRecord):
        """Record usage after task completion"""
        self.store.record_usage(record)

    def get_user_usage(self, user_id: str) -> UsageSnapshot | None:
        return self.store._user_usage.get(user_id)

    def get_system_usage(self) -> UsageSnapshot:
        return self.store._system_usage

    def get_dept_usage(self, dept_id: str) -> UsageSnapshot | None:
        return self.store._dept_usage.get(dept_id)

    # ── Real FIFO waiting queues ──

    def queue_size(self, user_id: str = "", department_id: str = "") -> int:
        """Get current queue size for a user or department"""
        if user_id:
            return self.store._queued_user_count.get(user_id, 0)
        if department_id:
            return self.store._queued_dept_count.get(department_id, 0)
        return 0

    def can_enqueue(self, user_id: str, department_id: str = "") -> bool:
        """Check if a new entry can be added to the queue (under max_queued)"""
        user_limit = self.limits[QuotaLevel.USER].max_queued
        if self.store._queued_user_count.get(user_id, 0) >= user_limit:
            return False
        if department_id:
            dept_limit = self.limits[QuotaLevel.DEPARTMENT].max_queued
            if self.store._queued_dept_count.get(department_id, 0) >= dept_limit:
                return False
        return True

    def enqueue(self, user_id: str, department_id: str = "") -> bool:
        """Add a task to the waiting queue. Returns True on success."""
        if not self.can_enqueue(user_id, department_id):
            return False
        if user_id not in self.store._user_queues:
            self.store._user_queues[user_id] = asyncio.Queue()
        self.store._user_queues[user_id].put_nowait(time.time())
        self.store._queued_user_count[user_id] = \
            self.store._queued_user_count.get(user_id, 0) + 1
        if department_id:
            if department_id not in self.store._dept_queues:
                self.store._dept_queues[department_id] = asyncio.Queue()
            self.store._dept_queues[department_id].put_nowait(time.time())
            self.store._queued_dept_count[department_id] = \
                self.store._queued_dept_count.get(department_id, 0) + 1
        return True

    def dequeue(self, user_id: str, department_id: str = "") -> bool:
        """Pop the next task off the queue (called when a slot frees up)."""
        popped = False
        if user_id in self.store._user_queues and not self.store._user_queues[user_id].empty():
            try:
                self.store._user_queues[user_id].get_nowait()
                popped = True
            except asyncio.QueueEmpty:
                pass
            self.store._queued_user_count[user_id] = max(
                0, self.store._queued_user_count.get(user_id, 0) - 1)
        if department_id and department_id in self.store._dept_queues and not self.store._dept_queues[department_id].empty():
            try:
                self.store._dept_queues[department_id].get_nowait()
                popped = popped or True
            except asyncio.QueueEmpty:
                pass
            self.store._queued_dept_count[department_id] = max(
                0, self.store._queued_dept_count.get(department_id, 0) - 1)
        return popped

    def notify_next(self, user_id: str = "", department_id: str = "") -> bool:
        """Called when a slot frees up. Returns True if a queued task was popped."""
        if self.queue_size(user_id, department_id) == 0:
            return False
        return self.dequeue(user_id, department_id)


# Global quota manager
quota_manager = QuotaManager()
