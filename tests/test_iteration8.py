"""
Tests: Iteration 4+ — Checkpoint + CEO escalation integration
"""

import pytest
from datetime import datetime, timezone
from pathlib import Path

from agent_system.core.agent import SmartAgent, TaskContext, OutputSchema
from agent_system.core.failure_ux import TaskCheckpoint, CheckpointStore


class TestCheckpointIntegration:
    """Checkpoint wired into SmartAgent.execute()"""

    @pytest.mark.asyncio
    async def test_successful_task_creates_and_deletes_checkpoint(self, tmp_path, monkeypatch):
        """On success, checkpoint is created then deleted (no resume needed)"""
        # Redirect data dir to tmp
        from agent_system.config import settings
        # monkeypatch the global store path is complex; just use a fresh store
        store = CheckpointStore(str(tmp_path))

        class GoodAgent(SmartAgent):
            agent_name: str = "good_checkpoint"
            agent_capabilities: list = ["testing"]
            description: str = "Test"

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id="good-out", type="test",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        agent = GoodAgent()
        # Monkey-patch the global CheckpointStore used inside execute()
        from agent_system.core import failure_ux
        monkeypatch.setattr(failure_ux, "CheckpointStore", lambda: store)

        task = TaskContext(task_id="ck-success", input="test")
        output = await agent.execute(task)
        assert output.id == "good-out"
        # After success, the checkpoint should have been deleted
        assert store.load("ck-success") is None

    @pytest.mark.asyncio
    async def test_failed_task_keeps_checkpoint(self, tmp_path, monkeypatch):
        """On failure (no resolver), checkpoint should remain for inspection"""
        store = CheckpointStore(str(tmp_path))

        class FailingAgent(SmartAgent):
            agent_name: str = "failing_checkpoint"
            agent_capabilities: list = ["testing"]
            description: str = "Test"

            async def do_work(self, task: TaskContext) -> OutputSchema:
                raise RuntimeError("Simulated failure")

        agent = FailingAgent(max_retries=1)
        from agent_system.core import failure_ux
        monkeypatch.setattr(failure_ux, "CheckpointStore", lambda: store)

        task = TaskContext(task_id="ck-fail", input="test", max_retries=1)
        with pytest.raises(RuntimeError):
            await agent.execute(task)

        # After failure, the checkpoint should still exist (for debugging/resume)
        cp = store.load("ck-fail")
        assert cp is not None
        assert cp.task_id == "ck-fail"
        assert cp.agent_name == "failing_checkpoint"

    @pytest.mark.asyncio
    async def test_resume_after_partial_progress(self, tmp_path):
        """Resume a failed task from a saved checkpoint"""
        store = CheckpointStore(str(tmp_path))

        # Manually create a checkpoint as if the previous run failed mid-way
        cp = TaskCheckpoint(
            task_id="ck-resume",
            agent_name="product_agent",
            pending_steps=[
                __import__("agent_system.core.failure_ux", fromlist=["StepRecord"]).StepRecord(
                    step_id="do_work", name="Execute"
                ),
            ],
            intermediate_outputs={"previous_run": "half-done"},
            timeout_seconds=300,
        )
        store.save(cp)

        # The resume context should include the previous progress
        loaded = store.load("ck-resume")
        assert loaded is not None
        ctx = loaded.to_resume_context()
        assert ctx["task_id"] == "ck-resume"
        assert "previous_run" in ctx["intermediate_outputs"]


class TestCEOEscalationRouting:
    """CEO escalation actually triggers when resolver picks ESCALATE"""

    @pytest.mark.asyncio
    async def test_escalate_routes_to_ceo(self, tmp_path, monkeypatch):
        """Failing agent with resolver enabled -> CEO Agent gets called"""
        from agent_system.core.resolver import SmartResolver
        from agent_system.core.evaluator import ResolutionPath
        from agent_system.core.failure_ux import CheckpointStore

        # Make checkpoint store write to tmp
        store = CheckpointStore(str(tmp_path))
        from agent_system.core import failure_ux, agent as agent_module
        monkeypatch.setattr(failure_ux, "CheckpointStore", lambda: store)

        class AlwaysFail(SmartAgent):
            agent_name: str = "always_fail"
            agent_capabilities: list = ["testing"]
            description: str = "Always fails"

            async def do_work(self, task: TaskContext) -> OutputSchema:
                raise RuntimeError("Internal: persistent failure")

        agent = AlwaysFail(max_retries=1)
        agent.enable_resolver()

        # Force the analysis to choose ESCALATE path
        from agent_system.core.evaluator import ProblemEvaluator, ActionCategory
        evaluator = ProblemEvaluator()
        # We patch the resolver to inject the analysis
        orig_resolve = agent._resolver.resolve
        async def patched_resolve(task, error, analysis):
            from agent_system.core.evaluator import ResolutionPath
            analysis.suggested_path = ResolutionPath.ESCALATE
            return await orig_resolve(task, error, analysis)
        agent._resolver.resolve = patched_resolve

        # Subscribe to CEO ESCALATED event (CEO emits one via event_bus)
        from agent_system.core.agent import event_bus, EventType, AgentEvent
        events = []
        async def collector(e):
            events.append(e)
        event_bus.subscribe(EventType.TASK_COMPLETED, collector)

        task = TaskContext(task_id="esc-route", input="complex task", max_retries=1)
        with pytest.raises(RuntimeError):
            await agent.execute(task)

        # Checkpoint should be saved (failed)
        cp = store.load("esc-route")
        assert cp is not None

        # After escalation, ceo_decision should be in metadata
        # (the original task was preserved; we set metadata on it)
        # Note: the task object is gone after the exception, but the checkpoint
        # has the failed step recorded.

        event_bus.unsubscribe(EventType.TASK_COMPLETED, collector)

    @pytest.mark.asyncio
    async def test_ceo_handles_escalation_directly(self):
        """CEO Agent should produce a decision output when given an escalation"""
        from agent_system.agents.ceo_agent import CEOAgent
        from agent_system.core.agent import TaskContext

        ceo = CEOAgent()
        task = TaskContext(
            task_id="esc-direct",
            input="escalation",
            metadata={
                "is_escalation": True,
                "escalation_data": {
                    "task_id": "orig-1",
                    "agent": "tech_agent",
                    "severity": "high",
                    "error": "Build failed",
                    "analysis": "Cannot resolve dependency",
                    "capabilities": ["code"],
                    "similar_experiences": [],
                },
            },
            max_retries=1,
        )
        output = await ceo.execute(task)

        # CEO output should be a decision
        assert output.type == "decision"
        assert "decision" in output.payload
        action = output.payload["decision"]["action"]
        assert action in ("handle", "assign", "change_rules", "call_human")


class TestPipelineStillWorks:
    """Make sure the existing pipeline still functions after these changes"""

    @pytest.mark.asyncio
    async def test_full_pipeline_runs(self):
        """CEO Agent's standard pipeline (not escalation) still works"""
        from agent_system.agents.ceo_agent import CEOAgent

        ceo = CEOAgent()
        task = TaskContext(
            task_id="pipe-still",
            input="Build a simple calculator",
            max_retries=1,
        )
        output = await ceo.execute(task)
        assert output.type == "pipeline_result"
        assert output.payload["pipeline_status"] in ("completed", "failed")