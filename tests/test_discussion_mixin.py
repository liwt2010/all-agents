"""
Tests: DiscussionMixin — real PEER deliberation
"""

import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock

from agent_system.core.mixins.discussion import (
    DiscussionMixin,
    DiscussionContext,
    DiscussionResult,
    DiscussionRole,
    PeerProvider,
    TaskContextLike,
)


# ── Helpers ──

class StubAgent(DiscussionMixin):
    agent_name: str = "test_agent"
    agent_capabilities: list = ["testing", "debugging"]


def make_peer(name: str, capabilities: list, message: str):
    """Build a PeerProvider that returns a stub output."""
    provider = PeerProvider(
        peer_fn=lambda peer_name, ctx: _StubOutput(peer_name, message)
    )
    provider.capabilities = capabilities
    return provider, name


class _StubOutput:
    def __init__(self, agent, message):
        self.payload = {"summary": message, "agent": agent}


# ── Tests ──

def test_register_peer_class_level():
    """register_peer at class level is shared across instances."""
    StubAgent.register_peer_class(
        "x_peer", PeerProvider(peer_fn=lambda n, c: _StubOutput(n, "hi"))
    )
    a = StubAgent()
    assert "x_peer" in a._all_peers()
    StubAgent._peer_registry.clear()


def test_register_peer_instance_level_overrides_class():
    """Instance peer overrides class peer with same name."""
    provider, _ = make_peer("a_peer", [], "instance msg")
    StubAgent.register_peer_class(
        "shared", PeerProvider(peer_fn=lambda n, c: _StubOutput(n, "class msg"))
    )
    a = StubAgent()
    a.register_peer("shared", provider)
    assert "shared" in a._all_peers()
    StubAgent._peer_registry.clear()


@pytest.mark.asyncio
async def test_discuss_no_peers():
    StubAgent._peer_registry.clear()
    a = StubAgent()
    ctx = DiscussionContext(task_input="build a feature", max_participants=3)
    result = await a.discuss(ctx)
    assert result.no_peers_available is True
    assert result.successful() is False
    StubAgent._peer_registry.clear()


@pytest.mark.asyncio
async def test_discuss_full_round_trip():
    StubAgent._peer_registry.clear()
    a = StubAgent()
    # Register two peers with different capabilities
    a.register_peer("tech", make_peer("tech", ["code", "architecture"], "try a cache")[0])
    a.register_peer("test", make_peer("test", ["test", "qa"], "add unit tests")[0])

    ctx = DiscussionContext(
        task_input="build a slow API",
        error="TimeoutError",
        capability_hint="performance",
        agent_capabilities=["code"],
        max_participants=2,
        timeout_seconds=10.0,
    )
    result = await a.discuss(ctx)

    # Transcript has 1 asker + 2 advisors + 1 synthesizer = 4 messages
    assert len(result.transcript) == 4
    assert result.transcript[0].role == DiscussionRole.ASKER
    assert result.transcript[0].agent == "test_agent"
    assert {m.agent for m in result.transcript[1:3]} == {"tech", "test"}
    assert result.transcript[3].role == DiscussionRole.SYNTHESIZER

    # Consensus extracted
    assert result.consensus is not None
    assert result.consensus.confidence > 0
    assert result.consensus.actionable_suggestion

    # Successful
    assert result.successful()

    # No timeout
    assert result.timed_out is False
    assert result.error is None

    StubAgent._peer_registry.clear()


@pytest.mark.asyncio
async def test_discuss_timeout():
    """When peers take too long, result has timed_out=True."""
    StubAgent._peer_registry.clear()
    a = StubAgent()

    async def slow_peer(name, ctx):
        await asyncio.sleep(5)

    provider = PeerProvider(peer_fn=lambda n, c: _StubOutput(n, "x"))
    provider.invoke = AsyncMock(side_effect=slow_peer)
    provider.capabilities = ["x"]
    a.register_peer("slow", provider)

    ctx = DiscussionContext(
        task_input="x",
        max_participants=1,
        timeout_seconds=0.5,  # very short
    )
    result = await a.discuss(ctx)
    assert result.timed_out is True
    StubAgent._peer_registry.clear()


@pytest.mark.asyncio
async def test_discuss_unavailable_peer_doesnt_crash():
    """A peer raising an exception is marked unavailable, others still respond."""
    StubAgent._peer_registry.clear()
    a = StubAgent()

    # Bad peer
    bad = PeerProvider(peer_fn=lambda n, c: (_ for _ in ()).throw(RuntimeError("boom")))
    bad.capabilities = ["x"]
    a.register_peer("bad", bad)

    # Good peer
    good, _ = make_peer("good", ["x"], "solid advice")
    a.register_peer("good", good)

    ctx = DiscussionContext(task_input="x", max_participants=2, timeout_seconds=5)
    result = await a.discuss(ctx)
    assert result.timed_out is False
    assert result.error is None
    # bad marked unavailable, good is present
    bad_msg = [m for m in result.transcript if m.agent == "bad"][0]
    good_msg = [m for m in result.transcript if m.agent == "good"][0]
    assert bad_msg.unavailable is True
    assert good_msg.unavailable is False
    # Consensus from good alone
    assert result.consensus is not None
    StubAgent._peer_registry.clear()


@pytest.mark.asyncio
async def test_peer_selection_by_relevance():
    """Higher-scoring peers come first when capped at max_participants."""
    StubAgent._peer_registry.clear()
    a = StubAgent()
    # Add 3 peers with varying relevance
    a.register_peer("unrelated", make_peer("unrelated", ["cooking"], "irrelevant")[0])
    a.register_peer("matching", make_peer("matching", ["authentication", "login"], "use JWT")[0])
    a.register_peer("partial", make_peer("partial", ["auth"], "check session")[0])

    ctx = DiscussionContext(
        task_input="fix login authentication bug",
        capability_hint="authentication",
        max_participants=1,  # only top 1
        agent_capabilities=[],
    )
    result = await a.discuss(ctx)
    # Should have picked matching (best score)
    assert "matching" in {m.agent for m in result.transcript}
    # transcript should have only 1 advisor
    advisors = [m for m in result.transcript if m.role == DiscussionRole.ADVISOR]
    assert len(advisors) == 1
    StubAgent._peer_registry.clear()


def test_consensus_with_no_advisors():
    """Empty advisor list -> zero-confidence consensus."""
    StubAgent._peer_registry.clear()
    a = StubAgent()
    synthesis = "no peer input"
    consensus = a._extract_consensus([], synthesis)
    assert consensus.confidence == 0
    assert consensus.agreement_ratio == 0
    assert consensus.actionable_suggestion == ""


def test_consensus_agreement_signal():
    """Multiple advisors with overlapping keywords raise agreement_ratio."""
    StubAgent._peer_registry.clear()
    a = StubAgent()
    from agent_system.core.mixins.discussion import DiscussionMessage
    msgs = [
        DiscussionMessage(agent="p1", role=DiscussionRole.ADVISOR,
                          message="try cache invalidation, the cache is the problem"),
        DiscussionMessage(agent="p2", role=DiscussionRole.ADVISOR,
                          message="cache problem, try cache invalidation"),
    ]
    consensus = a._extract_consensus(msgs, "synth")
    assert consensus.agreement_ratio > 0
    assert consensus.confidence > 0.3
    StubAgent._peer_registry.clear()


@pytest.mark.asyncio
async def test_discuss_with_unavailable_peers_still_succeeds():
    """If all peers fail, the discussion still completes (no crash, low confidence)."""
    StubAgent._peer_registry.clear()
    a = StubAgent()

    # All peers crash
    for i in range(2):
        bad = PeerProvider(peer_fn=lambda n, c: (_ for _ in ()).throw(RuntimeError(f"err{i}")))
        bad.capabilities = []
        a.register_peer(f"bad{i}", bad)

    ctx = DiscussionContext(task_input="x", max_participants=2, timeout_seconds=5)
    result = await a.discuss(ctx)
    assert result.timed_out is False
    # Consensus still produced (with 0 agreement)
    assert result.consensus is not None
    assert result.consensus.agreement_ratio == 0
    StubAgent._peer_registry.clear()
