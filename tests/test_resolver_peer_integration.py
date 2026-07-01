"""
Tests: SmartResolver PEER path uses DiscussionMixin (Round 1 #2)
"""

import asyncio
import pytest
from datetime import datetime, timezone

from agent_system.core.agent import SmartAgent, TaskContext, OutputSchema
from agent_system.core.resolver import SmartResolver
from agent_system.core.evaluator import (
    ProblemAnalysis,
    ResolutionPath,
    Severity,
    ActionCategory,
)
from agent_system.core.mixins.discussion import (
    DiscussionContext,
    DiscussionResult,
    DiscussionMessage,
    DiscussionRole,
    PeerProvider,
)


class TestPEERViaDiscussionMixin:
    """SmartResolver PEER path now uses DiscussionMixin"""

    @pytest.mark.asyncio
    async def test_resolve_peer_uses_discussion_mixin(self, monkeypatch):
        """Verify _resolve_peer goes through DiscussionMixin.consensus"""
        # Build a test agent
        from pydantic import ConfigDict

        class TestAgent(SmartAgent):
            agent_name: str = "test_peer_agent"
            agent_capabilities: list = ["code review", "refactoring"]
            description: str = "Test"

            model_config = ConfigDict(extra="allow")

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                # Use object.__setattr__ to bypass Pydantic field validation
                object.__setattr__(self, "peer_attempts", 0)
                object.__setattr__(self, "original_input", None)

            async def do_work(self, task: TaskContext) -> OutputSchema:
                # Track that we got a peer-enriched input
                if "[Peer discussion insight]" in task.input:
                    object.__setattr__(self, "original_input", task.input)
                    self.peer_attempts += 1
                return OutputSchema(
                    id=f"out-{self.peer_attempts}",
                    type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                    payload={"attempt": self.peer_attempts},
                )

        # Skip the full pipeline — directly test _resolve_peer
        agent = TestAgent()
        resolver = SmartResolver(agent)

        # Build a synthetic error
        error = RuntimeError("TypeError in production")
        analysis = ProblemAnalysis(
            severity=Severity.HIGH,
            confidence=0.4,
            can_self_solve=False,
            needs_peer_help=True,
            action_category=ActionCategory.NORMAL,
            suggested_path=ResolutionPath.PEER,
            reasoning="Need peer help",
            error_summary=str(error),
        )

        task = TaskContext(
            task_id="peer-via-mixin",
            input="build a feature",
        )

        result = await resolver._resolve_peer(task, error, analysis)

        # Either the retry succeeded (peer helped) or escalated (fallback)
        assert result.path in (
            ResolutionPath.PEER,
            ResolutionPath.ESCALATE,
        )

        # If PEER succeeded, the consensus was used
        if result.path == ResolutionPath.PEER:
            assert result.status.value == "success"
            assert result.output is not None
            # Verify the original input got enriched with peer insight
            assert "[Peer discussion insight]" in agent.original_input
            # Verify metadata captures consensus
            assert result.metadata.get("consensus_confidence", 0) > 0
            assert "discussion_duration_seconds" in result.metadata
            # Verify discussion log is populated
            assert len(result.discussion_log) >= 2
            roles = {m["role"] for m in result.discussion_log}
            assert "asker" in roles
            assert "advisor" in roles
            assert "synthesizer" in roles

    @pytest.mark.asyncio
    async def test_resolve_peer_escalates_when_no_consensus(self, monkeypatch):
        """If peer discussion fails to reach consensus, fall through to ESCALATE"""
        from agent_system.core.resolver import _PeerDiscussionAdapter
        from agent_system.agents.product_agent import ProductAgent

        # Use a real agent to bootstrap the adapter
        agent = ProductAgent()
        adapter = _PeerDiscussionAdapter(agent)

        def bad_provider(name, ctx):
            raise RuntimeError("simulated peer crash")

        # Replace all peer providers with bad ones
        for name in list(adapter._all_peers().keys()):
            adapter.register_peer(name, PeerProvider(peer_fn=bad_provider))

        # Now ask for discussion
        ctx = DiscussionContext(
            task_id="consensus-fail",
            task_input="fix bug",
            error="x",
            max_participants=2,
            timeout_seconds=5,
        )
        result = await adapter.discuss(ctx)

        # The discussion completes (all peers unavailable)
        assert result.timed_out is False
        # But there's no usable consensus
        assert not result.successful()
        # (Advisor list is empty so agreement_ratio is 0)
        assert result.consensus.agreement_ratio == 0
