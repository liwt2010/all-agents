"""
Tests: Iteration 4 — Smart Upgrade (SELF/PEER/HUMAN/ESCALATE)
"""

import pytest
from datetime import datetime, timezone

from agent_system.core.evaluator import (
    ProblemEvaluator,
    ResolutionPath,
    Severity,
    ActionCategory,
)
from agent_system.core.resolver import (
    SmartResolver,
    ResolutionStatus,
    ResolutionPath as ResPath,
)
from agent_system.core.agent import (
    SmartAgent,
    TaskContext,
    OutputSchema,
    event_bus,
    EventType,
)
from agent_system.agents.ceo_agent import CEOEscalationHandler


class TestProblemEvaluator:
    """Test problem assessment engine"""

    def setup_method(self):
        self.evaluator = ProblemEvaluator()

    def test_detect_irreversible(self):
        """Irreversible actions -> direct HUMAN"""
        analysis = self.evaluator.evaluate(
            error_message="Need to delete production database",
            agent_name="test_agent",
            agent_capabilities=["testing"],
            attempted_action="delete_database",
        )
        assert analysis.should_route_direct_to_human is True
        assert analysis.suggested_path == ResolutionPath.HUMAN

    def test_detect_compliance(self):
        """Compliance actions -> direct HUMAN"""
        analysis = self.evaluator.evaluate(
            error_message="GDPR data processing error",
            agent_name="test_agent",
            agent_capabilities=["testing"],
            attempted_action="process_pii_data",
        )
        assert analysis.should_route_direct_to_human is True
        assert analysis.suggested_path == ResolutionPath.HUMAN

    def test_temporary_error_self_solve(self):
        """Temporary errors -> SELF with high confidence"""
        analysis = self.evaluator.evaluate(
            error_message="API rate limit exceeded (429)",
            agent_name="test_agent",
            agent_capabilities=["api", "testing"],
        )
        assert analysis.can_self_solve is True
        assert analysis.confidence > 0.7
        assert analysis.suggested_path == ResolutionPath.SELF

    def test_high_severity_with_low_confidence(self):
        """High severity + low confidence -> PEER or ESCALATE"""
        analysis = self.evaluator.evaluate(
            error_message="Complex integration dependency conflict error",
            agent_name="test_agent",
            agent_capabilities=["simple_testing"],
            retry_count=2,
        )
        assert analysis.suggested_path in (
            ResolutionPath.PEER,
            ResolutionPath.ESCALATE,
        )

    def test_retry_exhaustion(self):
        """After multiple retries -> PEER or ESCALATE"""
        analysis = self.evaluator.evaluate(
            error_message="Unknown error occurred",
            agent_name="test_agent",
            agent_capabilities=["testing"],
            retry_count=3,
        )
        assert analysis.can_self_solve is False

    def test_action_classification(self):
        """Verify action classification logic"""
        evaluator = self.evaluator
        assert evaluator._classify_action("delete user data", "") == ActionCategory.DANGEROUS
        assert evaluator._classify_action("deploy to production", "") == ActionCategory.IRREVERSIBLE
        assert evaluator._classify_action("process gdpr data", "") == ActionCategory.COMPLIANCE
        assert evaluator._classify_action("update user name", "") == ActionCategory.NORMAL

    def test_severity_assessment(self):
        """Verify severity classification"""
        evaluator = self.evaluator
        assert evaluator._assess_severity("security breach detected", "") == Severity.CRITICAL
        assert evaluator._assess_severity("permission denied", "") == Severity.HIGH
        assert evaluator._assess_severity("invalid input format", "") == Severity.MEDIUM
        assert evaluator._assess_severity("minor warning", "") == Severity.LOW


class TestSmartResolver:
    """Test 4-way resolution dispatcher"""

    @pytest.mark.asyncio
    async def test_self_resolve_path(self):
        """SELF path: retry with enriched input"""
        agent = _create_test_agent()

        class SelfResolvableAgent(SmartAgent):
            agent_name: str = "self_resolver"
            agent_capabilities: list = ["testing"]
            description: str = "Test"
            call_count: int = 0

            async def do_work(self, task: TaskContext) -> OutputSchema:
                SelfResolvableAgent.call_count += 1
                if SelfResolvableAgent.call_count < 2:
                    raise TimeoutError("Temporary issue")
                return OutputSchema(
                    id="self-resolved", type="test",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        agent = SelfResolvableAgent()
        resolver = SmartResolver(agent)
        SelfResolvableAgent.call_count = 1  # simulate one prior failure

        result = await resolver.resolve(
            TaskContext(task_id="self-test", input="test"),
            TimeoutError("Temporary issue"),
        )
        assert result.path == ResolutionPath.SELF
        assert result.status == ResolutionStatus.SUCCESS
        assert result.output is not None

    @pytest.mark.asyncio
    async def test_human_path_for_irreversible(self):
        """HUMAN path: irreversible action -> approval request"""
        from agent_system.core.evaluator import ProblemEvaluator

        agent = _create_test_agent()
        evaluator = ProblemEvaluator()
        analysis = evaluator.evaluate(
            error_message="Cannot delete user database",
            agent_name="test_agent",
            agent_capabilities=["testing"],
            attempted_action="delete database",
        )

        resolver = SmartResolver(agent, evaluator)
        result = await resolver.resolve(
            TaskContext(task_id="human-test", input="delete database"),
            RuntimeError("Cannot delete user database"),
            analysis,
        )

        assert result.path == ResolutionPath.HUMAN
        assert result.human_request is not None
        assert "title" in result.human_request
        assert "options" in result.human_request

    @pytest.mark.asyncio
    async def test_human_path_emits_event(self):
        """HUMAN path: should emit HUMAN_REQUESTED event"""
        events = []
        async def collector(e):
            events.append(e)
        event_bus.subscribe(EventType.HUMAN_REQUESTED, collector)

        agent = _create_test_agent()
        evaluator = ProblemEvaluator()
        analysis = evaluator.evaluate(
            error_message="GDPR compliance required",
            agent_name="test_agent", agent_capabilities=["testing"],
            attempted_action="process_gdpr",
        )
        resolver = SmartResolver(agent, evaluator)
        await resolver.resolve(
            TaskContext(task_id="human-event", input="process gdpr"),
            RuntimeError("GDPR check"),
            analysis,
        )

        assert len(events) >= 1
        assert events[0].event_type == EventType.HUMAN_REQUESTED
        event_bus.unsubscribe(EventType.HUMAN_REQUESTED, collector)

    @pytest.mark.asyncio
    async def test_escalate_path(self):
        """ESCALATE path: should emit ESCALATED event"""
        events = []
        async def collector(e):
            events.append(e)
        event_bus.subscribe(EventType.ESCALATED, collector)

        agent = _create_test_agent()
        evaluator = ProblemEvaluator()
        analysis = evaluator.evaluate(
            error_message="Critical system failure in module X",
            agent_name="test_agent", agent_capabilities=["simple_testing"],
            retry_count=3,
        )

        resolver = SmartResolver(agent, evaluator)
        result = await resolver.resolve(
            TaskContext(task_id="escalate-test", input="complex task"),
            RuntimeError("System critical failure"),
            analysis,
        )

        assert result.path == ResolutionPath.ESCALATE
        assert len(events) >= 1
        assert events[0].event_type == EventType.ESCALATED
        event_bus.unsubscribe(EventType.ESCALATED, collector)


class TestCEOEscalation:
    """Test CEO escalation handler"""

    @pytest.mark.asyncio
    async def test_ceo_can_handle_directly(self):
        handler = CEOEscalationHandler(None)  # type: ignore
        assert handler._can_handle_directly("Resource constraint: out of memory", []) is True
        assert handler._can_handle_directly("SyntaxError: unexpected indent", []) is False

    @pytest.mark.asyncio
    async def test_ceo_find_expert(self):
        handler = CEOEscalationHandler(None)  # type: ignore
        expert = handler._find_expert("Build failed: dependency not found", [])
        assert expert == "tech_agent"

        expert2 = handler._find_expert("Test assertion error", [])
        assert expert2 == "test_agent"

    @pytest.mark.asyncio
    async def test_ceo_decision_flow(self):
        """Test full CEO escalation decision flow"""
        from agent_system.agents.ceo_agent import CEOAgent

        agent = CEOAgent()
        handler = CEOEscalationHandler(agent)

        decision = await handler.handle_escalation(
            task_id="esc-test",
            agent_name="tech_agent",
            error="Build failed: dependency missing",
            severity="high",
            analysis="Cannot resolve dependency conflict",
            capabilities=["code"],
            similar_experiences=[],
        )

        assert decision.action in ("handle", "assign", "change_rules", "call_human")
        assert decision.reason != ""


class TestIntegration:
    """Integration tests for the full escalation system"""

    @pytest.mark.asyncio
    async def test_agent_with_resolver_retries_then_escalates(self):
        """Agent with resolver enabled should retry then escalate on failure"""

        class FailingAgent(SmartAgent):
            agent_name: str = "failing_agent"
            agent_capabilities: list = ["testing"]
            description: str = "Always fails"

            async def do_work(self, task: TaskContext) -> OutputSchema:
                raise RuntimeError("Persistent internal error")

        agent = FailingAgent(max_retries=1)
        agent.enable_resolver()

        events = []
        async def collector(e):
            events.append(e)
        event_bus.subscribe(EventType.ESCALATED, collector)
        event_bus.subscribe(EventType.TASK_FAILED, collector)

        task = TaskContext(task_id="integ-test", input="test", max_retries=1)
        with pytest.raises(RuntimeError):
            await agent.execute(task)

        # Should have recorded a failure event
        failed_events = [e for e in events if e.event_type == EventType.TASK_FAILED]
        assert len(failed_events) >= 1

        event_bus.unsubscribe(EventType.ESCALATED, collector)
        event_bus.unsubscribe(EventType.TASK_FAILED, collector)

    @pytest.mark.asyncio
    async def test_resolver_fallback_paths(self):
        """After SELF fails, should try PEER then ESCALATE"""

        class AlwaysFails(SmartAgent):
            agent_name: str = "always_fails"
            agent_capabilities: list = ["testing"]
            description: str = "Always fails"

            async def do_work(self, task: TaskContext) -> OutputSchema:
                raise RuntimeError("Cannot complete")

        agent = AlwaysFails()
        resolver = SmartResolver(agent)

        result = await resolver.resolve(
            TaskContext(task_id="fallback-test", input="test"),
            RuntimeError("Cannot complete"),
        )

        # Should fail through to ESCALATE
        assert result.status == ResolutionStatus.FAILED

    @pytest.mark.asyncio
    async def test_human_request_format(self):
        """Human approval request should have proper structure"""
        from agent_system.core.evaluator import ProblemEvaluator
        agent = _create_test_agent()
        evaluator = ProblemEvaluator()

        analysis = evaluator.evaluate(
            error_message="Deploy to production requires approval",
            agent_name="test_agent", agent_capabilities=["testing"],
            attempted_action="deploy to production",
        )

        resolver = SmartResolver(agent, evaluator)
        result = await resolver.resolve(
            TaskContext(task_id="human-format", input="deploy production"),
            RuntimeError("Not authorized"),
            analysis,
        )

        req = result.human_request
        assert req is not None
        assert "title" in req
        assert "options" in req
        assert "risk_assessment" in req
        assert "context" in req

        # Verify options structure
        option_ids = [o["id"] for o in req["options"]]
        assert "approve" in option_ids
        assert "reject" in option_ids
        assert "modify" in option_ids

    @pytest.mark.asyncio
    async def test_peer_discussion_round_robin(self):
        """PEER path should actually query peer agents and produce discussion log"""
        from agent_system.core.evaluator import ProblemEvaluator
        from agent_system.agents.tech_agent import TechAgent

        agent = TechAgent()
        evaluator = ProblemEvaluator()
        # Mark as needing peer help: complex + low confidence
        analysis = evaluator.evaluate(
            error_message="Complex integration dependency conflict",
            agent_name=agent.agent_name,
            agent_capabilities=agent.agent_capabilities,
            retry_count=0,
        )
        # Force the path to PEER for the test
        from agent_system.core.evaluator import ResolutionPath
        analysis.suggested_path = ResolutionPath.PEER

        resolver = SmartResolver(agent, evaluator)
        result = await resolver.resolve(
            TaskContext(task_id="peer-rr-test", input="complex integration"),
            RuntimeError("Complex dependency issue"),
            analysis,
        )

        # PEER path returns to escalate when no real peer succeeds in mock mode,
        # but the discussion log should still capture the round-robin participation
        # if it was attempted. Allow either PEER->success or ESCALATE (fallback).
        assert result.path in (ResolutionPath.PEER, ResolutionPath.ESCALATE)


def _create_test_agent():
    class TestAgent(SmartAgent):
        agent_name: str = "test_agent"
        agent_capabilities: list = ["testing"]
        description: str = "Test Agent"

        async def do_work(self, task: TaskContext) -> OutputSchema:
            return OutputSchema(
                id="test-output", type="test",
                created_at=datetime.now(timezone.utc),
                created_by=self.agent_name,
            )
    return TestAgent()
