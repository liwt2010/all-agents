"""
Live task progress tracker for the API server.

Wraps CheckpointStore with an in-memory map for currently-running tasks
so /api/tasks/{id}/progress can return real-time status without hitting disk.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from agent_system.core.failure_ux import TaskCheckpoint, StepRecord, CheckpointStore

logger = logging.getLogger(__name__)


class LiveProgress(BaseModel):
    """Lightweight status snapshot for the API."""
    task_id: str
    status: str = "pending"  # pending / running / completed / failed
    progress: float = 0.0     # 0.0 to 1.0
    current_step: str = ""
    current_step_id: str = ""
    completed_steps: List[str] = Field(default_factory=list)
    pending_steps: List[str] = Field(default_factory=list)
    error: Optional[str] = None
    started_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    output: Optional[Dict[str, Any]] = None
    retry_count: int = 0
    tenant_id: str = "default"


class CheckpointTracker:
    """
    In-memory tracker for active task progress.

    Persists checkpoints to disk via CheckpointStore, but also keeps a
    hot in-memory map so the API can serve real-time progress.
    """

    def __init__(self, store: Optional[CheckpointStore] = None):
        self.store = store or CheckpointStore()
        self._live: Dict[str, TaskCheckpoint] = {}

    # ── Lifecycle ──

    _tenant_map: Dict[str, str] = {}  # task_id -> tenant_id

    def start(self, task_id: str, agent_name: str, task_input: str = "",
              tenant_id: str = "default") -> TaskCheckpoint:
        """Begin tracking a new task with a single 'do_work' step."""
        self._tenant_map[task_id] = tenant_id
        cp = TaskCheckpoint(
            task_id=task_id,
            agent_name=agent_name,
            task_type=self._estimate_type(task_input),
            pending_steps=[
                StepRecord(
                    step_id="do_work",
                    name=f"Run {agent_name}",
                    started_at=datetime.now(timezone.utc),
                )
            ],
            timeout_seconds=300,
        )
        cp.start()
        self._live[task_id] = cp
        self.store.save(cp)
        return cp

    def complete_step(self, task_id: str, step_id: str, output: Optional[Dict[str, Any]] = None):
        """Mark a step as completed (and update output)."""
        cp = self._live.get(task_id)
        if not cp:
            return
        cp.complete_step(step_id, output)
        if output is not None:
            cp.intermediate_outputs[step_id] = output
        cp.updated_at = datetime.now(timezone.utc)
        self.store.save(cp)

    def add_step(self, task_id: str, step_id: str, name: str) -> bool:
        """Add a new step (e.g. for multi-step pipelines)."""
        cp = self._live.get(task_id)
        if not cp:
            return False
        cp.pending_steps.append(StepRecord(
            step_id=step_id,
            name=name,
            started_at=datetime.now(timezone.utc),
        ))
        cp.updated_at = datetime.now(timezone.utc)
        self.store.save(cp)
        return True

    def fail_step(self, task_id: str, step_id: str, error: str):
        cp = self._live.get(task_id)
        if not cp:
            return
        cp.fail_step(step_id, error)
        cp.updated_at = datetime.now(timezone.utc)
        self.store.save(cp)

    def finish(self, task_id: str, success: bool = True, output: Optional[Dict[str, Any]] = None):
        """Mark the entire task as complete. Removes from hot map, persists result."""
        cp = self._live.get(task_id)
        if cp:
            if success:
                cp.complete_step("do_work", output)
            cp.updated_at = datetime.now(timezone.utc)
            self.store.save(cp)
            # Keep the in-memory entry for a short while so the API can
            # return final status to the next poll, then evict
        self._live.pop(task_id, None)

    def get_live(self, task_id: str) -> Optional[LiveProgress]:
        """Get current progress for a task (live or recently completed)."""
        cp = self._live.get(task_id)
        if cp:
            # A task is "running" if it has any pending steps; otherwise completed
            has_pending = any(s.status != "completed" for s in cp.pending_steps)
            has_failed = any(s.status == "failed" for s in cp.completed_steps)
            if has_failed:
                return self._to_progress(cp, status="failed")
            status = "running" if has_pending else "completed"
            return self._to_progress(cp, status=status)
        # Try the persisted store for a recent run
        persisted = self.store.load(task_id)
        if persisted:
            has_failed = any(s.status == "failed" for s in persisted.completed_steps)
            has_pending = any(s.status != "completed" for s in persisted.pending_steps)
            if has_failed:
                return self._to_progress(persisted, status="failed")
            status = "running" if has_pending else "completed"
            return self._to_progress(persisted, status=status)
        return None

    def list_active(self) -> List[str]:
        return list(self._live.keys())

    # ── Helpers ──

    def _to_progress(self, cp: TaskCheckpoint, status: str) -> LiveProgress:
        current_step = ""
        current_step_id = ""
        for step in cp.pending_steps:
            if step.status != "completed":
                current_step = step.name
                current_step_id = step.step_id
                break
        # If everything is complete, set progress=1.0
        progress = cp.progress
        if not cp.pending_steps:
            progress = 1.0
        last_error = None
        if cp.error_history:
            last_error = cp.error_history[-1].get("error")
        tenant_id = self._tenant_map.get(cp.task_id, "default")
        return LiveProgress(
            task_id=cp.task_id,
            status=status,
            progress=progress,
            current_step=current_step,
            current_step_id=current_step_id,
            completed_steps=[s.step_id for s in cp.completed_steps],
            pending_steps=[s.step_id for s in cp.pending_steps],
            error=last_error,
            started_at=cp.started_at,
            updated_at=cp.updated_at,
            output=cp.intermediate_outputs.get("do_work"),
            tenant_id=tenant_id,
        )
    @staticmethod
    def _estimate_type(text: str) -> str:
        """Rough task type estimate (for timeout selection)."""
        text_lower = (text or "").lower()
        if any(kw in text_lower for kw in ["batch", "bulk", "mass"]):
            return "batch"
        if any(kw in text_lower for kw in ["complex", "end-to-end", "pipeline"]):
            return "long"
        if any(kw in text_lower for kw in ["design", "multi-step"]):
            return "complex"
        if len(text) < 50:
            return "quick"
        return "standard"


# Global tracker (used by the API server)
tracker = CheckpointTracker()
