"""
Tests: Failure UX stage 5 (notification + resume)
"""

import pytest
import asyncio
import time

from agent_system.core.notify import (
    Notifier,
    Notification,
    NotificationChannel,
    ResumeManager,
    ResumeStatus,
    ResumeOffer,
    get_notifier,
    get_resume_manager,
)
from agent_system.core.failure_ux import TaskCheckpoint, StepRecord, CheckpointStore


class TestNotifier:
    def setup_method(self):
        self.notifier = Notifier()

    def test_in_app_notification(self):
        n = self.notifier.notify(
            title="Task failed",
            body="Something went wrong",
            severity="error",
            channel=NotificationChannel.IN_APP,
            related_task_id="t-1",
        )
        assert n.id.startswith("n-")
        assert n.read is False
        inbox = self.notifier.get_inbox()
        assert len(inbox) == 1
        assert inbox[0].title == "Task failed"

    def test_notify_task_failure_convenience(self):
        notes = self.notifier.notify_task_failure(
            task_id="t-fail",
            error="RuntimeError: oops",
            agent_name="tech_agent",
        )
        # Goes to both IN_APP and LOG by default
        assert len(notes) == 2
        assert all(n.related_task_id == "t-fail" for n in notes)

    def test_mark_read(self):
        n = self.notifier.notify("t", "b", channel=NotificationChannel.IN_APP)
        assert n.read is False
        assert self.notifier.mark_read(n.id) is True
        assert n.read is True

    def test_get_inbox_unread_only(self):
        self.notifier.notify("a", "b", channel=NotificationChannel.IN_APP)
        self.notifier.notify("c", "d", channel=NotificationChannel.IN_APP)
        # Mark first as read
        inbox = self.notifier.get_inbox()
        self.notifier.mark_read(inbox[0].id)
        # Unread only has 1 left
        unread = self.notifier.get_inbox(unread_only=True)
        assert len(unread) == 1
        assert unread[0].title == "c"

    def test_get_inbox_by_recipient(self):
        self.notifier.notify("a", "b", channel=NotificationChannel.IN_APP, recipient="alice")
        self.notifier.notify("c", "d", channel=NotificationChannel.IN_APP, recipient="bob")
        alice = self.notifier.get_inbox(recipient="alice")
        assert len(alice) == 1
        assert alice[0].recipient == "alice"

    def test_custom_handler(self):
        received = []
        self.notifier.register_handler(
            NotificationChannel.WEBHOOK,
            lambda n: received.append(n),
        )
        self.notifier.notify("x", "y", channel=NotificationChannel.WEBHOOK)
        assert len(received) == 1
        assert received[0].title == "x"

    def test_handler_exception_doesnt_break(self):
        def bad(n):
            raise RuntimeError("boom")
        self.notifier.register_handler(NotificationChannel.WEBHOOK, bad)
        # Should not raise
        n = self.notifier.notify("x", "y", channel=NotificationChannel.WEBHOOK)
        assert n.id  # Still returned

    def test_singleton(self):
        n1 = get_notifier()
        n2 = get_notifier()
        assert n1 is n2


class TestResumeManager:
    def setup_method(self, tmp_path_factory):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.store = CheckpointStore(self.tmp)
        self.manager = ResumeManager(checkpoint_store=self.store)

    def _make_checkpoint(self, task_id: str = "t-1"):
        cp = TaskCheckpoint(
            task_id=task_id,
            agent_name="tech_agent",
            pending_steps=[
                StepRecord(step_id="do_work", name="Run", status="pending"),
            ],
            completed_steps=[
                StepRecord(step_id="setup", name="Setup", status="completed"),
            ],
        )
        cp.start()
        cp.complete_step("setup")
        self.store.save(cp)

    def test_create_offer_no_checkpoint(self):
        offer = self.manager.create_offer("nonexistent", "err")
        assert offer is None

    def test_create_offer_with_checkpoint(self):
        self._make_checkpoint("t-1")
        offer = self.manager.create_offer("t-1", "RuntimeError: oops")
        assert offer is not None
        assert offer.task_id == "t-1"
        assert offer.error == "RuntimeError: oops"
        assert offer.status == ResumeStatus.PENDING
        assert "setup" in offer.steps_completed
        assert "do_work" in offer.steps_pending

    def test_get_offer(self):
        self._make_checkpoint("t-1")
        offer = self.manager.create_offer("t-1", "err")
        fetched = self.manager.get_offer("t-1")
        assert fetched is offer

    def test_accept_offer(self):
        self._make_checkpoint("t-1")
        offer = self.manager.create_offer("t-1", "err")
        accepted = self.manager.accept_offer("t-1", offer.resume_token)
        assert accepted.status == ResumeStatus.RESUMED

    def test_accept_offer_wrong_token(self):
        self._make_checkpoint("t-1")
        self.manager.create_offer("t-1", "err")
        accepted = self.manager.accept_offer("t-1", "wrong-token")
        assert accepted is None

    def test_abandon_offer(self):
        self._make_checkpoint("t-1")
        self.manager.create_offer("t-1", "err")
        assert self.manager.abandon_offer("t-1") is True
        assert self.manager.get_offer("t-1").status == ResumeStatus.ABANDONED

    def test_build_resume_payload(self):
        self._make_checkpoint("t-1")
        offer = self.manager.create_offer("t-1", "err")
        # Not accepted yet — no payload
        payload = self.manager.build_resume_payload("t-1")
        assert payload is None

        # Accept
        self.manager.accept_offer("t-1", offer.resume_token)
        payload = self.manager.build_resume_payload("t-1")
        assert payload is not None
        assert payload["task_id"] == "t-1"
        assert "setup" in payload["completed_steps"]
        assert payload.get("resume_from_step") == "do_work"

    def test_list_offers_by_status(self):
        self._make_checkpoint("t-1")
        self._make_checkpoint("t-2")
        self._make_checkpoint("t-3")
        self.manager.create_offer("t-1", "e1")
        self.manager.create_offer("t-2", "e2")
        self.manager.create_offer("t-3", "e3")
        self.manager.abandon_offer("t-2")
        pending = self.manager.list_offers(status=ResumeStatus.PENDING)
        abandoned = self.manager.list_offers(status=ResumeStatus.ABANDONED)
        assert len(pending) == 2
        assert len(abandoned) == 1

    def test_singleton(self):
        r1 = get_resume_manager()
        r2 = get_resume_manager()
        assert r1 is r2
