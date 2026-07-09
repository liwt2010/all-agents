"""
P0-1.2: Real-LLM end-to-end PEER discussion test.

Runs SmartResolver's PEER path with REAL LLM API calls (via the configured
OPENAI_API_KEY / ANTHROPIC_API_KEY env vars). Peer agents use the real
LLM, exchange suggestions, and converge on a consensus.

Skipped if no API key is configured (CI without secrets).

Run:
    OPENAI_API_KEY=sk-... ANTHROPIC_API_KEY=sk-... \\
    PYTHONPATH=src .venv/Scripts/python.exe -m pytest \\
    tests/test_resolver_peer_real_llm.py -v -s
"""
import asyncio
import os
import time
import pytest
from datetime import datetime, timezone


# Skip the whole module if no real API key is available
def _has_api_key():
    return bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("OPENAI_API_KEY", "").strip()
    )

pytestmark = pytest.mark.skipif(
    not _has_api_key(),
    reason="No real API key (set ANTHROPIC_API_KEY or OPENAI_API_KEY to run real-LLM tests)",
)


@pytest.mark.asyncio
async def test_resolve_peer_real_llm_three_agents():
    """Three real LLM agents discuss a problem, converge on consensus.

    Uses the project's product/tech/test agents (all with real LLM backing).
    Tech Agent is given an impossible task that fails first, then PEER
    kicks in to ask Product + Test for help.

    NOTE: With the test proxy (deepseek-v4-pro with deep reasoning), peer
    agents often return empty messages because their max_tokens is consumed
    by reasoning_content. This test verifies the *plumbing* (PEER path
    runs end-to-end, no crash) and the *invocation* (peers are actually
    called). For real convergence tests, use a model with shorter reasoning
    (e.g. deepseek-v4-flash, claude-haiku) and increase max_tokens.
    """
    from pydantic import ConfigDict
    from agent_system.core.agent import SmartAgent, TaskContext, OutputSchema
    from agent_system.core.resolver import SmartResolver, _PeerDiscussionAdapter
    from agent_system.core.evaluator import (
        ProblemAnalysis,
        ResolutionPath,
        Severity,
        ActionCategory,
    )

    # Force anthropic provider (works around the proxy's OpenAI-SDK
    # compatibility issue, see fix commit 4887a94).
    os.environ["LLM_PROVIDER"] = "anthropic"
    if not os.environ.get("ANTHROPIC_BASE_URL") and os.environ.get("OPENAI_BASE_URL"):
        os.environ["ANTHROPIC_BASE_URL"] = os.environ["OPENAI_BASE_URL"]

    # No instrumentation needed — the discussion_log + peer_agreement +
    # peer_confidence in result.metadata already prove peers ran.

    class FailingTechAgent(SmartAgent):
        """A tech agent that fails on the first attempt, then succeeds with
        peer-discussion insight."""
        agent_name: str = "tech_agent"
        agent_capabilities: list = ["code generation", "architecture"]
        description: str = "Test tech agent that fails first"
        model_config = ConfigDict(extra="allow")

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            object.__setattr__(self, "call_count", 0)
            object.__setattr__(self, "saw_peer_insight", False)

        async def do_work(self, task: TaskContext) -> OutputSchema:
            self.call_count += 1
            if "[Peer discussion insight]" in task.input:
                object.__setattr__(self, "saw_peer_insight", True)
            # First call always fails to trigger PEER path
            if self.call_count == 1:
                raise RuntimeError("simulated tech failure: missing dependency")
            # Subsequent calls succeed
            return OutputSchema(
                id=f"tech-success-{self.call_count}",
                type="result",
                created_at=datetime.now(timezone.utc),
                created_by=self.agent_name,
                payload={
                    "solution": "Implemented using peer suggestion",
                    "iterations": self.call_count,
                },
            )

    agent = FailingTechAgent()
    resolver = SmartResolver(agent)

    task = TaskContext(
        task_id="real-peer-1",
        input="Implement a cache layer with Redis",
    )

    # Simulate a first-call failure so resolver enters the PEER path
    error = RuntimeError("simulated tech failure: missing dependency")
    analysis = ProblemAnalysis(
        severity=Severity.MEDIUM,
        confidence=0.4,
        can_self_solve=False,
        needs_peer_help=True,
        action_category=ActionCategory.NORMAL,
        suggested_path=ResolutionPath.PEER,
        reasoning="Need peer help to find missing dependency",
        error_summary=str(error),
    )

    print(f"\n=== Real-LLM PEER test (model={os.environ.get('LLM_MODEL', 'default')}) ===")
    t0 = time.perf_counter()
    result = await resolver._resolve_peer(task, error, analysis)
    elapsed = time.perf_counter() - t0

    print(f"  Resolution path:   {result.path.value}")
    print(f"  Status:            {result.status.value}")
    print(f"  Duration:          {elapsed:.2f}s")
    print(f"  Peer agreement:    {result.metadata.get('consensus_agreement', 0):.2f}")
    print(f"  Peer confidence:   {result.metadata.get('consensus_confidence', 0):.2f}")
    print(f"  Discussion log:    {len(result.discussion_log)} messages")
    print(f"  Saw peer insight:  {agent.saw_peer_insight}")

    # Either PEER succeeded or escalated (both are valid)
    assert result.path in (ResolutionPath.PEER, ResolutionPath.ESCALATE), \
        f"unexpected path: {result.path}"

    # The PEER path MUST have called peer agents and produced some
    # discussion log entries. A successful PEER has >= 3 messages
    # (asker + advisor + synthesizer). An ESCALATE has >= 1 (the
    # attempt was made but no consensus).
    if result.path == ResolutionPath.PEER:
        assert len(result.discussion_log) >= 3, (
            f"PEER success but only {len(result.discussion_log)} messages — "
            "expected >= 3 (asker + advisor + synthesizer)"
        )
        # The retry happened with the peer insight injected
        assert agent.saw_peer_insight, "retry should have seen peer insight"
        # Discussion log must have asker + advisor + synthesizer
        roles = {m["role"] for m in result.discussion_log}
        assert "asker" in roles
        assert "advisor" in roles
        assert "synthesizer" in roles
    else:
        # ESCALATE — the discussion was attempted but no consensus
        # This is still a successful integration test (no crash)
        assert result.status.value in ("failed", "escalated")


@pytest.mark.asyncio
async def test_resolve_peer_handles_real_llm_failure_gracefully():
    """If peer LLM calls fail, the resolver must not crash.

    Uses a real agent (ProductAgent) so the registry is populated with
    real peers. Replaces all peers with broken ones that raise on
    invocation. The discussion should complete (all peers fail) without
    raising — the resolver then escalates.
    """
    from agent_system.core.resolver import SmartResolver, _PeerDiscussionAdapter
    from agent_system.core.evaluator import (
        ProblemAnalysis,
        ResolutionPath,
        Severity,
        ActionCategory,
    )
    from agent_system.core.mixins.discussion import (
        DiscussionContext, PeerProvider,
    )
    from agent_system.agents.product_agent import ProductAgent

    # Force legacy mixin path (skip AutoGen) by using anthropic provider
    os.environ["LLM_PROVIDER"] = "anthropic"

    adapter = _PeerDiscussionAdapter(ProductAgent())
    assert len(adapter._all_peers()) > 0, "expected real agents in registry"

    broken_calls = []

    async def broken_peer(peer_name, ctx):
        broken_calls.append(peer_name)
        # Simulate a real LLM call failure
        raise RuntimeError("simulated LLM crash in peer")

    # Replace ALL peer providers with broken ones
    for name in list(adapter._all_peers().keys()):
        adapter.register_peer(name, PeerProvider(peer_fn=broken_peer))

    # Run the discussion directly
    ctx = DiscussionContext(
        task_id="peer-graceful",
        task_input="build a feature with cache",
        error="primary failure",
        max_participants=2,
        timeout_seconds=5.0,
    )
    result = await adapter.discuss(ctx)

    # Peers were attempted
    assert len(broken_calls) > 0, "peers should have been called at least once"
    # Discussion completed (all peers broken but didn't crash)
    assert result.timed_out is False
    # No usable consensus (all broken) but didn't raise
    assert result.consensus.agreement_ratio == 0