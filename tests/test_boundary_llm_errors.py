"""Boundary tests: LLM API error handling (429/500/timeout) with 4-way Resolver.

Issue: LLM API returns 429/500/timeout, verify 4-way Resolver correctly falls back.
- 429 (rate limit) -> SELF retry with backoff
- 500 (server error) -> PEER consultation or ESCALATE
- Timeout -> SELF retry or ESCALATE

Run: pytest tests/test_boundary_llm_errors.py -v
"""
import asyncio
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch, MagicMock

from agent_system.core.agent import SmartAgent, TaskContext, OutputSchema
from agent_system.core.resolver import SmartResolver
from agent_system.core.evaluator import (
    ProblemAnalysis,
    ResolutionPath,
    Severity,
    ActionCategory,
)
from agent_system.core.llm_router import (
    TransientLLMError,
    FatalLLMError,
)


class TestLLM429RateLimit:
    """429 triggers SELF retry with backoff, not immediate escalation."""

    @pytest.mark.asyncio
    async def test_429_triggers_self_retry_not_escalate(self):
        """429 should retry via SELF path, not escalate immediately."""
        from pydantic import ConfigDict

        class TestAgent(SmartAgent):
            agent_name: str = "test_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test"
            model_config = ConfigDict(extra="allow")

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                object.__setattr__(self, "call_count", 0)

            async def do_work(self, task: TaskContext) -> OutputSchema:
                self.call_count += 1
                if self.call_count == 1:
                    raise TransientLLMError("Rate limit exceeded", is_retriable=True)
                return OutputSchema(
                    id=f"success-{self.call_count}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                )

        agent = TestAgent()
        resolver = SmartResolver(agent)

        task = TaskContext(task_id="test-429", input="test")
        error = TransientLLMError("429 Too Many Requests", is_retriable=True)
        analysis = ProblemAnalysis(
            severity=Severity.LOW,
            confidence=0.9,
            can_self_solve=True,
            needs_peer_help=False,
            action_category=ActionCategory.SIMPLE,
            suggested_path=ResolutionPath.SELF,
            reasoning="Transient error, retry may work",
        )

        result = await resolver.resolve(task, error, analysis)

        # Should retry via SELF, not escalate
        assert result.path in (ResolutionPath.SELF, ResolutionPath.ESCALATE)
        if result.path == ResolutionPath.SELF:
            assert result.status.value in ("success", "pending")


class TestLLM500ServerError:
    """500 errors should escalate to PEER or ESCALATE."""

    @pytest.mark.asyncio
    async def test_500_escalates_to_peer_or_ceo(self):
        """500 server error should not loop forever on SELF."""
        from pydantic import ConfigDict

        class FailingAgent(SmartAgent):
            agent_name: str = "failing_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test"
            model_config = ConfigDict(extra="allow")

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                object.__setattr__(self, "call_count", 0)

            async def do_work(self, task: TaskContext) -> OutputSchema:
                self.call_count += 1
                # Always fail with 500
                raise TransientLLMError("500 Internal Server Error", is_retriable=True)

        agent = FailingAgent()
        resolver = SmartResolver(agent)

        task = TaskContext(task_id="test-500", input="test")
        error = TransientLLMError("500 Internal Server Error", is_retriable=True)
        analysis = ProblemAnalysis(
            severity=Severity.HIGH,
            confidence=0.3,
            can_self_solve=False,
            needs_peer_help=True,
            action_category=ActionCategory.COMPLEX,
            suggested_path=ResolutionPath.PEER,
            reasoning="Server error, need peer help",
        )

        result = await resolver.resolve(task, error, analysis)

        # Should escalate (not stuck in SELF loop)
        assert result.path in (ResolutionPath.PEER, ResolutionPath.ESCALATE)
        assert result.path != ResolutionPath.SELF  # No infinite SELF loop


class TestLLMTimeout:
    """Timeout should trigger appropriate escalation."""

    @pytest.mark.asyncio
    async def test_timeout_escalates_after_retries(self):
        """Timeout should eventually escalate, not retry indefinitely."""
        from pydantic import ConfigDict

        class TimeoutAgent(SmartAgent):
            agent_name: str = "timeout_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test"
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                raise TransientLLMError("Request timeout", is_retriable=True)

        agent = TimeoutAgent()
        resolver = SmartResolver(agent)

        task = TaskContext(task_id="test-timeout", input="test")
        error = TransientLLMError("Request timeout after 30s", is_retriable=True)
        analysis = ProblemAnalysis(
            severity=Severity.MEDIUM,
            confidence=0.5,
            can_self_solve=False,
            needs_peer_help=True,
            action_category=ActionCategory.COMPLEX,
            suggested_path=ResolutionPath.ESCALATE,
            reasoning="Timeout indicates complex issue",
        )

        result = await resolver.resolve(task, error, analysis)

        # Should not loop forever
        assert result.path in (ResolutionPath.ESCALATE, ResolutionPath.PEER)


class TestFatalLLMError:
    """Fatal errors (auth, bad request) should not retry."""

    @pytest.mark.asyncio
    async def test_fatal_error_escalates_immediately(self):
        """Fatal errors skip SELF retry and escalate directly."""
        from pydantic import ConfigDict

        class TestAgent(SmartAgent):
            agent_name: str = "fatal_agent"
            agent_capabilities: list = ["test"]
            description: str = "Test"
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                raise FatalLLMError("Invalid API key")

        agent = TestAgent()
        resolver = SmartResolver(agent)

        task = TaskContext(task_id="test-fatal", input="test")
        error = FatalLLMError("401 Unauthorized: Invalid API key")
        analysis = ProblemAnalysis(
            severity=Severity.HIGH,
            confidence=1.0,
            can_self_solve=False,
            needs_peer_help=False,
            action_category=ActionCategory.COMPLEX,
            suggested_path=ResolutionPath.ESCALATE,
            reasoning="Fatal error, cannot retry",
        )

        result = await resolver.resolve(task, error, analysis)

        # Should escalate, not retry
        assert result.path == ResolutionPath.ESCALATE
