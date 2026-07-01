"""
Failure UX — 5-stage error handling + TaskCheckpoint + Timeout policies

ARCHITECTURE.md Ch.12:
  Stage 1: Progress (progress bar + live log + cancel)
  Stage 2: Failure (friendly error + category + suggestion)
  Stage 3: Retry (one-click retry + change params + change agent)
  Stage 4: Record (auto-enter reflection store)
  Stage 5: Notify (close the loop)

ARCHITECTURE.md Ch.13: Timeout policies
  quick/standard/complex/long/batch
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from agent_system.core.event_bus import (
    EventBus,
    EventCategory,
    EventSeverity,
    make_event,
    event_bus as global_bus,
)

logger = logging.getLogger(__name__)


# ── Timeout Policies ──

class TaskType(str, Enum):
    QUICK = "quick"       # 1 min
    STANDARD = "standard" # 5 min
    COMPLEX = "complex"   # 30 min
    LONG = "long"         # 2 hours
    BATCH = "batch"       # 1 day


TIMEOUT_SECONDS = {
    TaskType.QUICK: 60,
    TaskType.STANDARD: 300,
    TaskType.COMPLEX: 1800,
    TaskType.LONG: 7200,
    TaskType.BATCH: 86400,
}

TIMEOUT_LABELS = {
    TaskType.QUICK: "1 minute",
    TaskType.STANDARD: "5 minutes",
    TaskType.COMPLEX: "30 minutes",
    TaskType.LONG: "2 hours",
    TaskType.BATCH: "24 hours",
}


def get_timeout(task_type: TaskType) -> int:
    return TIMEOUT_SECONDS.get(task_type, 300)


def estimate_task_type(task_input: str) -> TaskType:
    """Estimate task type based on input description"""
    input_lower = task_input.lower()

    if any(kw in input_lower for kw in ["batch", "bulk", "mass", "all users", "full"]):
        return TaskType.BATCH
    if any(kw in input_lower for kw in ["complex", "comprehensive", "end-to-end", "full pipeline", "migration"]):
        return TaskType.LONG
    if any(kw in input_lower for kw in ["design", "architecture", "multi-step", "pipeline"]):
        return TaskType.COMPLEX
    if any(kw in input_lower for kw in ["quick", "simple", "tiny", "small"]) or len(task_input) < 50:
        return TaskType.QUICK

    return TaskType.STANDARD


# ── Error Facing (Friendly Errors) ──

class ErrorCategory(str, Enum):
    NETWORK = "network"           # Connection errors, timeouts
    VALIDATION = "validation"     # Input validation failures
    PERMISSION = "permission"     # Access denied
    CAPABILITY = "capability"     # Agent lacks capability
    RESOURCE = "resource"         # Resource not found/quota exceeded
    INTERNAL = "internal"         # Internal system error
    LLM = "llm"                   # LLM API errors


ERROR_SUGGESTIONS = {
    ErrorCategory.NETWORK: [
        "Check your network connection",
        "The service may be temporarily unavailable — try again later",
        "Reduce request frequency to avoid rate limits",
    ],
    ErrorCategory.VALIDATION: [
        "Review the input parameters for correctness",
        "Check that all required fields are provided",
        "Ensure the input format matches the expected schema",
    ],
    ErrorCategory.PERMISSION: [
        "Verify you have the required permissions",
        "Request access from your administrator",
        "Try a different action that requires fewer permissions",
    ],
    ErrorCategory.CAPABILITY: [
        "This task requires capabilities not available to the current agent",
        "Try assigning this task to a different agent with more expertise",
        "Break the task into smaller, more focused subtasks",
    ],
    ErrorCategory.RESOURCE: [
        "The requested resource may not exist or has been moved",
        "Check resource identifiers for typos",
        "Your quota may be exceeded — request a limit increase",
    ],
    ErrorCategory.INTERNAL: [
        "An internal system error occurred",
        "Try again — the issue may be transient",
        "If the problem persists, contact support",
    ],
    ErrorCategory.LLM: [
        "The AI model may be temporarily unavailable",
        "Try simplifying the request",
        "Reduce the response length or break the task into smaller parts",
    ],
}


class FriendlyError(BaseModel):
    """User-friendly error representation"""
    title: str
    message: str
    category: ErrorCategory
    suggestions: List[str] = Field(default_factory=list)
    can_retry: bool = True
    can_change_agent: bool = False
    error_code: str = ""
    details: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_exception(cls, e: Exception, task_input: str = "") -> "FriendlyError":
        """Create from an exception"""
        error_str = str(e).lower()

        if any(kw in error_str for kw in ["timeout", "connection", "network", "refused", "reset"]):
            cat = ErrorCategory.NETWORK
        elif any(kw in error_str for kw in ["permission", "denied", "forbidden", "unauthorized"]):
            cat = ErrorCategory.PERMISSION
        elif any(kw in error_str for kw in ["not found", "does not exist", "missing"]):
            cat = ErrorCategory.RESOURCE
        elif any(kw in error_str for kw in ["invalid", "validation", "bad request"]):
            cat = ErrorCategory.VALIDATION
        elif any(kw in error_str for kw in ["rate limit", "quota", "limit"]):
            cat = ErrorCategory.NETWORK
        elif any(kw in error_str for kw in ["capability", "cannot", "unable"]):
            cat = ErrorCategory.CAPABILITY
        elif any(kw in error_str for kw in ["api", "model", "token", "anthropic"]):
            cat = ErrorCategory.LLM
        else:
            cat = ErrorCategory.INTERNAL

        return cls(
            title=cat.value.replace("_", " ").title(),
            message=str(e)[:300],
            category=cat,
            suggestions=ERROR_SUGGESTIONS.get(cat, ["Try again later"]),
            can_retry=cat in (ErrorCategory.NETWORK, ErrorCategory.LLM, ErrorCategory.INTERNAL),
            can_change_agent=cat == ErrorCategory.CAPABILITY,
            error_code=f"ERR-{cat.value.upper()}",
        )


# ── Task Checkpoint (ARCHITECTURE.md 12.2) ──

class StepRecord(BaseModel):
    """Record of a task step"""
    step_id: str
    name: str
    status: str = "pending"  # pending / running / completed / failed / skipped
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    output: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class TaskCheckpoint(BaseModel):
    """
    Checkpoint — saves progress so tasks can resume after failure.

    Architecture: every completed step saves intermediate state.
    If the task fails, it can resume from the last checkpoint.
    """
    task_id: str
    agent_name: str
    task_type: TaskType = TaskType.STANDARD
    completed_steps: List[StepRecord] = Field(default_factory=list)
    pending_steps: List[StepRecord] = Field(default_factory=list)
    intermediate_outputs: Dict[str, Any] = Field(default_factory=dict)
    error_history: List[Dict[str, Any]] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    can_resume: bool = True
    timeout_seconds: int = 300
    _deadline: Optional[float] = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def start(self):
        """Mark the checkpoint as started with deadline"""
        self._deadline = time.time() + self.timeout_seconds

    @property
    def is_expired(self) -> bool:
        """Check if this task has exceeded its timeout"""
        if self._deadline is None:
            return False
        return time.time() > self._deadline

    @property
    def remaining_seconds(self) -> int:
        if self._deadline is None:
            return self.timeout_seconds
        remaining = int(self._deadline - time.time())
        return max(0, remaining)

    @property
    def progress(self) -> float:
        """Progress as a ratio (0.0 - 1.0)"""
        total = len(self.completed_steps) + len(self.pending_steps)
        if total == 0:
            return 0.0
        return len(self.completed_steps) / total

    def complete_step(self, step_id: str, output: Optional[Dict[str, Any]] = None):
        """Mark a step as completed"""
        for step in self.pending_steps:
            if step.step_id == step_id:
                step.status = "completed"
                step.completed_at = datetime.now(timezone.utc)
                step.output = output or {}
                self.completed_steps.append(step)
                self.pending_steps.remove(step)
                self.updated_at = datetime.now(timezone.utc)
                return
        # Also check completed steps
        for step in self.completed_steps:
            if step.step_id == step_id:
                step.output = output or {}
                return

    def fail_step(self, step_id: str, error: str):
        """Mark a step as failed"""
        for step in self.pending_steps:
            if step.step_id == step_id:
                step.status = "failed"
                step.error = error
                self.completed_steps.append(step)
                self.pending_steps.remove(step)
                self.updated_at = datetime.now(timezone.utc)
                self.error_history.append({
                    "step_id": step_id,
                    "error": error,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                return

    def add_error(self, error: str):
        """Log an error without failing a specific step"""
        self.error_history.append({
            "step_id": "general",
            "error": error,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        self.updated_at = datetime.now(timezone.utc)

    def to_resume_context(self) -> Dict[str, Any]:
        """Generate context for resuming this task"""
        return {
            "task_id": self.task_id,
            "completed": [s.step_id for s in self.completed_steps],
            "pending": [s.step_id for s in self.pending_steps],
            "intermediate_outputs": self.intermediate_outputs,
            "error_history": self.error_history[-3:],  # last 3 errors
            "remaining_seconds": self.remaining_seconds,
        }


class CheckpointStore:
    """Persistent storage for checkpoints"""

    def __init__(self, store_dir: str = "data/checkpoints"):
        self.store_dir = Path(store_dir)

    def save(self, checkpoint: TaskCheckpoint) -> bool:
        """Save checkpoint to disk"""
        import json
        self.store_dir.mkdir(parents=True, exist_ok=True)
        path = self.store_dir / f"{checkpoint.task_id}.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(checkpoint.model_dump_json(indent=2))
            return True
        except Exception as e:
            logger.warning(f"Failed to save checkpoint {checkpoint.task_id}: {e}")
            return False

    def load(self, task_id: str) -> Optional[TaskCheckpoint]:
        """Load checkpoint from disk"""
        import json
        path = self.store_dir / f"{task_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
            return TaskCheckpoint(**data)
        except Exception as e:
            logger.warning(f"Failed to load checkpoint {task_id}: {e}")
            return None

    def delete(self, task_id: str) -> bool:
        """Delete checkpoint"""
        path = self.store_dir / f"{task_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    def list_active(self) -> List[str]:
        """List all active checkpoint task IDs"""
        if not self.store_dir.exists():
            return []
        return [p.stem for p in self.store_dir.glob("*.json")]


# ── Timeout Monitor ──

class TimeoutMonitor:
    """Monitors tasks for timeout and triggers escalation"""

    def __init__(self, event_bus: Optional[EventBus] = None):
        self._checkpoints: Dict[str, TaskCheckpoint] = {}
        self._event_bus = event_bus or global_bus

    def register(self, checkpoint: TaskCheckpoint):
        self._checkpoints[checkpoint.task_id] = checkpoint

    def check_expired(self) -> List[str]:
        """Return list of expired task IDs"""
        expired = []
        for tid, cp in self._checkpoints.items():
            if cp.is_expired:
                expired.append(tid)
        return expired

    async def handle_expired(self, task_id: str) -> bool:
        """Handle an expired task. Returns True if handled."""
        cp = self._checkpoints.get(task_id)
        if not cp:
            return False

        event = make_event(
            category=EventCategory.SYSTEM,
            name="task.timeout",
            source="timeout_monitor",
            data={
                "task_id": task_id,
                "agent": cp.agent_name,
                "timeout": cp.timeout_seconds,
                "progress": cp.progress,
            },
            severity=EventSeverity.WARNING,
        )
        await self._event_bus.publish(event)
        logger.warning(f"Task {task_id} timed out after {cp.timeout_seconds}s")
        return True


# ── UX Stage Helpers ──

class StageProgress(BaseModel):
    """Current stage progress info"""
    stage: int  # 1-5
    label: str
    message: str
    progress: float = 0.0  # 0-1
    can_cancel: bool = True
    task_id: str = ""


def format_stage(stage: int, message: str, progress: float = 0.0) -> StageProgress:
    """Create a stage progress entry"""
    labels = {
        1: "Executing",
        2: "Failed",
        3: "Retrying",
        4: "Recording",
        5: "Notifying",
    }
    return StageProgress(
        stage=stage,
        label=labels.get(stage, "Unknown"),
        message=message,
        progress=progress,
    )
