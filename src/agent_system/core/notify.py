"""
Failure UX — Stage 5 (notification + resume) — PLATFORM §30.1

The 5-stage failure UX:
  1. Progress     — done (in-progress endpoint + UI bar)
  2. Failure      — done (FriendlyError with category + suggestions)
  3. Retry        — done (one-click retry / change params / change agent)
  4. Record       — done (auto into reflection graph)
  5. Notify       — THIS MODULE: notification + resume checkpoint

This module adds:
  - Notifier: dispatches notifications (in-app, log, email-stub, webhook)
  - ResumeManager: tracks failed tasks + their checkpoint, allows resume
"""

import logging
import time
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict

from agent_system.core.failure_ux import (
    TaskCheckpoint,
    CheckpointStore,
)
from agent_system.core.security import AuditLogEntry, audit_logger

logger = logging.getLogger(__name__)


class NotificationChannel(str, Enum):
    IN_APP = "in_app"
    EMAIL = "email"
    WEBHOOK = "webhook"
    LOG = "log"


class Notification(BaseModel):
    """A single notification."""
    model_config = ConfigDict(extra="allow")

    id: str = ""
    channel: NotificationChannel = NotificationChannel.IN_APP
    recipient: str = ""
    title: str = ""
    body: str = ""
    severity: str = "info"  # info / warning / error / critical
    related_task_id: str = ""
    related_checkpoint_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    read: bool = False


class Notifier:
    """
    Sends notifications when tasks fail. Backed by the audit log
    + an in-memory inbox for in-app notifications.
    """

    def __init__(self):
        self._inbox: list[Notification] = []
        self._handlers: dict[NotificationChannel, list[Callable]] = {
            n: [] for n in NotificationChannel
        }
        self._handlers[NotificationChannel.LOG].append(self._log_handler)
        self._handlers[NotificationChannel.IN_APP].append(self._in_app_handler)

    def register_handler(self, channel: NotificationChannel, handler: Callable):
        self._handlers[channel].append(handler)

    def notify(
        self,
        title: str,
        body: str,
        severity: str = "info",
        channel: NotificationChannel = NotificationChannel.IN_APP,
        recipient: str = "",
        related_task_id: str = "",
        related_checkpoint_id: str = "",
    ) -> Notification:
        n = Notification(
            id=f"n-{int(time.time() * 1000)}",
            channel=channel,
            recipient=recipient,
            title=title,
            body=body,
            severity=severity,
            related_task_id=related_task_id,
            related_checkpoint_id=related_checkpoint_id,
        )
        for handler in self._handlers.get(channel, []):
            try:
                handler(n)
            except Exception as e:
                logger.warning(f"Notification handler failed: {e}")
        return n

    def notify_task_failure(
        self,
        task_id: str,
        error: str,
        agent_name: str = "",
        severity: str = "warning",
    ) -> list[Notification]:
        """Notify about a task failure across all relevant channels."""
        notifications = []
        for ch in [NotificationChannel.IN_APP, NotificationChannel.LOG]:
            n = self.notify(
                title=f"Task failed: {task_id}",
                body=f"Agent {agent_name} failed: {error[:300]}",
                severity=severity,
                channel=ch,
                related_task_id=task_id,
            )
            notifications.append(n)
        return notifications

    def get_inbox(
        self,
        recipient: str | None = None,
        unread_only: bool = False,
        limit: int = 50,
    ) -> list[Notification]:
        items = [
            n for n in self._inbox
            if (recipient is None or n.recipient == recipient)
            and (not unread_only or not n.read)
        ]
        return items[-limit:]

    def mark_read(self, notification_id: str) -> bool:
        for n in self._inbox:
            if n.id == notification_id:
                n.read = True
                return True
        return False

    def _log_handler(self, n: Notification) -> None:
        try:
            audit_logger.log(AuditLogEntry(
                user_id=n.recipient or "system",
                action="notification",
                resource_type="notification",
                resource_id=n.id,
                details={
                    "title": n.title,
                    "body": n.body,
                    "severity": n.severity,
                    "related_task_id": n.related_task_id,
                },
            ))
        except Exception as e:
            logger.debug(f"Audit log write failed: {e}")

    def _in_app_handler(self, n: Notification) -> None:
        self._inbox.append(n)


# ── ResumeManager — stage 5 ──

class ResumeStatus(str, Enum):
    PENDING = "pending"
    RESUMED = "resumed"
    ABANDONED = "abandoned"
    EXPIRED = "expired"


class ResumeOffer(BaseModel):
    """An offer to resume a failed task."""
    model_config = ConfigDict(extra="allow")

    task_id: str
    checkpoint_id: str
    error: str
    failed_at: datetime
    can_resume: bool
    resume_token: str
    status: ResumeStatus = ResumeStatus.PENDING
    steps_completed: list[str] = Field(default_factory=list)
    steps_pending: list[str] = Field(default_factory=list)


class ResumeManager:
    """
    Tracks failed tasks + their checkpoints. When a task fails and a
    checkpoint is saved, a ResumeOffer is created and the user can
    later resume from the last good state.
    """

    def __init__(self, checkpoint_store: CheckpointStore | None = None):
        self.store = checkpoint_store or CheckpointStore()
        self._offers: dict[str, ResumeOffer] = {}

    def create_offer(
        self,
        task_id: str,
        error: str,
    ) -> ResumeOffer | None:
        """Create a resume offer if a checkpoint exists for this task."""
        cp = self.store.load(task_id)
        if not cp:
            return None
        offer = ResumeOffer(
            task_id=task_id,
            checkpoint_id=f"cp-{task_id}",
            error=error[:500],
            failed_at=cp.updated_at,
            can_resume=any(s.status != "completed" for s in cp.pending_steps) or
                       bool(cp.completed_steps),
            resume_token=f"resume-{task_id}-{int(time.time())}",
            steps_completed=[s.step_id for s in cp.completed_steps if s.status == "completed"],
            steps_pending=[s.step_id for s in cp.pending_steps if s.status != "completed"],
        )
        self._offers[task_id] = offer
        return offer

    def get_offer(self, task_id: str) -> ResumeOffer | None:
        return self._offers.get(task_id)

    def list_offers(
        self,
        status: ResumeStatus | None = None,
    ) -> list[ResumeOffer]:
        offers = list(self._offers.values())
        if status:
            offers = [o for o in offers if o.status == status]
        return offers

    def accept_offer(
        self,
        task_id: str,
        resume_token: str,
    ) -> ResumeOffer | None:
        """User accepts the resume offer."""
        offer = self._offers.get(task_id)
        if not offer or offer.resume_token != resume_token:
            return None
        offer.status = ResumeStatus.RESUMED
        return offer

    def abandon_offer(self, task_id: str) -> bool:
        offer = self._offers.get(task_id)
        if not offer:
            return False
        offer.status = ResumeStatus.ABANDONED
        return True

    def build_resume_payload(self, task_id: str) -> dict[str, Any] | None:
        """Build a payload the calling code can use to restart a task."""
        offer = self._offers.get(task_id)
        if not offer or offer.status != ResumeStatus.RESUMED:
            return None
        cp = self.store.load(task_id)
        if not cp:
            return None
        return {
            "task_id": task_id,
            "resume_from_step": (
                offer.steps_pending[0] if offer.steps_pending else None
            ),
            "completed_steps": offer.steps_completed,
            "intermediate_outputs": cp.intermediate_outputs,
            "resume_token": offer.resume_token,
        }


# Global instances
_default_notifier: Notifier | None = None
_default_resume: ResumeManager | None = None


def get_notifier() -> Notifier:
    global _default_notifier
    if _default_notifier is None:
        _default_notifier = Notifier()
    return _default_notifier


def get_resume_manager() -> ResumeManager:
    global _default_resume
    if _default_resume is None:
        _default_resume = ResumeManager()
    return _default_resume
