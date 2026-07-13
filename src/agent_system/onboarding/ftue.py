"""
FTUE (First-Time User Experience) — PLATFORM §17

The 7-step flow that gets new users to first success in <30 seconds.
We implement the backend state machine + sample task suggestions.
The frontend /onboarding page consumes this data.

Steps:
  1. (0s)   auto-account on first visit
  2. (5s)   show sample task input
  3.        user picks a sample or types
  4.        real-time progress
  5. (30s)  result
  6.        celebratory prompt
  7.        branch (continue / tutorial / no thanks)
"""

import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class FTUEStep(str, Enum):
    """The 7-step FTUE flow."""
    AUTO_ACCOUNT = "auto_account"
    SHOW_SAMPLES = "show_samples"
    USER_INPUT = "user_input"
    SHOW_PROGRESS = "show_progress"
    SHOW_RESULT = "show_result"
    CELEBRATE = "celebrate"
    BRANCH = "branch"


class FTUESampleTask(BaseModel):
    """A sample task suggestion shown to new users."""
    id: str
    title: str
    description: str
    agent: str = "product"
    sample_input: str
    expected_output: str  # "JSON PRD with features", etc.


# Default sample tasks (PLATFORM §17.2)
DEFAULT_SAMPLES = [
    FTUESampleTask(
        id="sample-1",
        title="Write a product description",
        description="Get AI to write a description of your product",
        agent="product",
        sample_input="Write a one-paragraph description of a task management app for software teams",
        expected_output="A polished product description paragraph",
    ),
    FTUESampleTask(
        id="sample-2",
        title="Analyze sample data",
        description="Get AI to explore a CSV or analyze trends",
        agent="ceo",
        sample_input="Build a simple todo app with login",
        expected_output="A multi-step product/tech/test/deploy plan",
    ),
    FTUESampleTask(
        id="sample-3",
        title="Design a login screen",
        description="Generate a UI/UX design with components",
        agent="product",
        sample_input="Design a clean login screen with email + password, including error states",
        expected_output="A spec with layout, components, and accessibility notes",
    ),
]


class FTUEState(BaseModel):
    """Tracks a single user's FTUE progress."""
    user_id: str
    tenant_id: str = "default"
    current_step: FTUEStep = FTUEStep.AUTO_ACCOUNT
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    sample_picked_id: str | None = None
    first_task_id: str | None = None
    choice: str | None = None  # "continue" / "tutorial" / "skip"

    @property
    def time_to_value_seconds(self) -> float | None:
        """Time from FTUE start to first task completion (TTV metric)."""
        if not self.completed_at:
            return None
        return (self.completed_at - self.started_at).total_seconds()


class FTUEManager:
    """
    Manages FTUE state for new users.

    The frontend drives the user through the steps. This class provides:
      - The default sample tasks to display
      - State persistence per user
      - TTV metric recording
    """

    DEFAULT_TTV_TARGET = 30.0  # seconds

    def __init__(self):
        self._states: dict[str, FTUEState] = {}  # user_id -> state

    def get_or_create(self, user_id: str, tenant_id: str = "default") -> FTUEState:
        key = self._key(user_id, tenant_id)
        if key not in self._states:
            self._states[key] = FTUEState(user_id=user_id, tenant_id=tenant_id)
        return self._states[key]

    def get_state(self, user_id: str, tenant_id: str = "default") -> FTUEState | None:
        return self._states.get(self._key(user_id, tenant_id))

    def advance(self, user_id: str, step: FTUEStep, tenant_id: str = "default") -> FTUEState:
        state = self.get_or_create(user_id, tenant_id)
        state.current_step = step
        if step == FTUEStep.SHOW_RESULT and state.completed_at is None:
            state.completed_at = datetime.now(timezone.utc)
        return state

    def record_choice(
        self,
        user_id: str,
        choice: str,
        tenant_id: str = "default",
    ) -> FTUEState:
        state = self.get_or_create(user_id, tenant_id)
        state.choice = choice
        return state

    def get_samples(self) -> list[FTUESampleTask]:
        return DEFAULT_SAMPLES

    def get_sample(self, sample_id: str) -> FTUESampleTask | None:
        for s in DEFAULT_SAMPLES:
            if s.id == sample_id:
                return s
        return None

    def stats(self) -> dict[str, Any]:
        """Aggregate FTUE statistics for monitoring."""
        total = len(self._states)
        completed = sum(1 for s in self._states.values() if s.completed_at)
        ttv_values = [
            s.time_to_value_seconds for s in self._states.values()
            if s.time_to_value_seconds is not None
        ]
        avg_ttv = sum(ttv_values) / len(ttv_values) if ttv_values else None
        meets_target = sum(1 for t in ttv_values if t <= self.DEFAULT_TTV_TARGET)
        return {
            "total_users": total,
            "completed": completed,
            "ftue_completion_rate": completed / total if total else 0,
            "avg_ttv_seconds": avg_ttv,
            "ttv_target_seconds": self.DEFAULT_TTV_TARGET,
            "meets_ttv_target_pct": meets_target / len(ttv_values) if ttv_values else 0,
        }

    def _key(self, user_id: str, tenant_id: str) -> str:
        return f"{tenant_id}::{user_id}"


# Global
_default_manager: FTUEManager | None = None


def get_ftue_manager() -> FTUEManager:
    global _default_manager
    if _default_manager is None:
        _default_manager = FTUEManager()
    return _default_manager
