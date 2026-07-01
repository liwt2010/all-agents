"""
Tests: Iteration 9 — Live progress endpoint + CheckpointTracker
"""

import pytest
from datetime import datetime, timezone

from agent_system.core.checkpoint_tracker import (
    CheckpointTracker,
    LiveProgress,
)


class TestCheckpointTracker:
    """Test the in-memory progress tracker"""

    def setup_method(self):
        self.tracker = CheckpointTracker()

    def test_start_creates_checkpoint(self):
        cp = self.tracker.start("task-1", "test_agent", "build a feature")
        assert cp.task_id == "task-1"
        assert cp.agent_name == "test_agent"
        assert len(cp.pending_steps) == 1
        assert cp.pending_steps[0].step_id == "do_work"

    def test_complete_step_updates_progress(self):
        self.tracker.start("task-2", "test_agent", "test")
        self.tracker.complete_step("task-2", "do_work", output={"result": "ok"})

        progress = self.tracker.get_live("task-2")
        assert progress is not None
        assert progress.progress == 1.0
        assert "do_work" in progress.completed_steps
        assert progress.status == "completed"

    def test_get_live_returns_running_for_active_task(self):
        self.tracker.start("task-3", "test_agent", "test")
        progress = self.tracker.get_live("task-3")
        assert progress is not None
        assert progress.status == "running"
        assert progress.current_step == "Run test_agent"
        assert progress.current_step_id == "do_work"

    def test_finish_marks_complete_and_evicts(self):
        self.tracker.start("task-4", "test_agent", "test")
        self.tracker.finish("task-4", success=True, output={"id": "out-1"})

        # Should not be in hot map anymore
        assert "task-4" not in self.tracker.list_active()

        # But persisted, so we can still query it
        progress = self.tracker.get_live("task-4")
        assert progress is not None
        assert progress.status == "completed"
        assert progress.progress == 1.0

    def test_fail_step(self):
        self.tracker.start("task-5", "test_agent", "test")
        self.tracker.fail_step("task-5", "do_work", "Some error")

        progress = self.tracker.get_live("task-5")
        assert progress is not None
        assert progress.error == "Some error"

    def test_add_step_for_multi_step(self):
        self.tracker.start("task-6", "ceo_agent", "build pipeline")
        self.tracker.add_step("task-6", "tech_step", "Implement code")
        self.tracker.add_step("task-6", "test_step", "Run tests")

        progress = self.tracker.get_live("task-6")
        assert progress is not None
        assert "tech_step" in progress.pending_steps
        assert "test_step" in progress.pending_steps

    def test_estimate_type(self):
        assert CheckpointTracker._estimate_type("short") == "quick"
        assert CheckpointTracker._estimate_type("do batch processing for all users") == "batch"
        assert CheckpointTracker._estimate_type("complex end-to-end migration") == "long"
        # Use a longer input that crosses the 50-char boundary
        assert CheckpointTracker._estimate_type("a normal task with enough content to qualify as standard type for sure here") == "standard"

    def test_get_live_nonexistent(self):
        assert self.tracker.get_live("nonexistent") is None

    def test_list_active(self):
        self.tracker.start("a", "agent", "task a")
        self.tracker.start("b", "agent", "task b")
        active = self.tracker.list_active()
        assert "a" in active
        assert "b" in active

    def test_progress_serialization(self):
        self.tracker.start("serial-1", "agent", "task")
        progress = self.tracker.get_live("serial-1")
        # Verify it serializes to JSON (datetime fields)
        d = progress.model_dump(mode="json")
        assert "task_id" in d
        assert "progress" in d
        assert "started_at" in d


class TestProgressEndpoint:
    """Test the API endpoint /api/tasks/{id}/progress"""

    def test_progress_for_completed_task(self):
        from fastapi.testclient import TestClient
        from agent_system.api.server import app, _checkpoint_tracker

        from agent_system.core.auth import get_auth_service
        svc = get_auth_service()
        token = svc.issue_token("alice", tenant_id="acme")
        auth_hdr = {"Authorization": f"Bearer {token}"}

        client = TestClient(app)
        r = client.post("/api/tasks", json={
            "input": "test progress endpoint",
            "agent": "product",
        }, headers=auth_hdr)
        assert r.status_code == 200
        task_id = r.json()["task_id"]

        p = client.get(f"/api/tasks/{task_id}/progress", headers=auth_hdr)
        assert p.status_code == 200
        data = p.json()
        assert data["task_id"] == task_id
        assert data["status"] in ("completed", "running", "failed")
        assert "progress" in data
        assert "completed_steps" in data
        assert "pending_steps" in data

    def test_progress_for_unknown_task(self):
        from fastapi.testclient import TestClient
        from agent_system.api.server import app

        from agent_system.core.auth import get_auth_service
        svc = get_auth_service()
        token = svc.issue_token("alice", tenant_id="acme")
        client = TestClient(app)
        r = client.get("/api/tasks/never-existed-xyz/progress", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 404
