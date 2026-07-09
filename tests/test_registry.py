"""
Tests: AgentRegistry (PR 5 / registry 化)

Verifies AgentRegistry auto-discovery + resolver uses registry (not hardcoded list).
"""

import pytest

from agent_system.core.agent import SmartAgent, TaskContext, OutputSchema
from datetime import datetime, timezone
from pydantic import ConfigDict


_AGENT_CLASSES = (
    "ProductAgent", "TechAgent", "TestAgent", "CEOAgent",
    "DeployAgent", "DevOpsAgent", "DocsAgent", "ReviewAgent",
    "SecurityAgent",
)


@pytest.fixture(autouse=True)
def fresh_registry():
    """Reset the registry between tests so state doesn't leak.

    IMPORTANT: We do NOT use `importlib.reload()` — that would re-execute the
    agent module bodies, replacing the class objects and invalidating class
    refs held by other modules (discussion mixin, peer discovery, etc.).
    Instead we just reset() the registry and re-register the existing classes.
    This keeps class identity stable while giving each test a fresh registry.
    """
    from agent_system.agents import agent_registry, register_agent
    agent_registry.reset()
    # Re-register the already-imported classes (idempotent, no reload needed)
    from agent_system.agents import (
        ProductAgent, TechAgent, TestAgent, CEOAgent,
        DeployAgent, DevOpsAgent, DocsAgent, ReviewAgent, SecurityAgent,
    )
    for cls in (ProductAgent, TechAgent, TestAgent, CEOAgent,
                DeployAgent, DevOpsAgent, DocsAgent, ReviewAgent, SecurityAgent):
        register_agent(cls)
    yield


class TestAgentRegistry:
    """AgentRegistry: auto-discovery + dynamic peer resolution"""

    def test_all_9_agents_registered(self):
        from agent_system.agents import agent_registry
        names = set(agent_registry.all_names())
        expected = {
            "ceo_agent", "product_agent", "tech_agent", "test_agent",
            "deploy_agent", "devops_agent", "docs_agent", "review_agent",
            "security_agent",
        }
        assert expected.issubset(names), f"Missing: {expected - names}"

    def test_names_excluding_self(self):
        from agent_system.agents import agent_registry
        names = agent_registry.names_excluding("tech_agent")
        assert "tech_agent" not in names
        assert "product_agent" in names
        assert "ceo_agent" in names

    def test_get_instance_returns_cached(self):
        from agent_system.agents import agent_registry
        a1 = agent_registry.get_instance("product_agent")
        a2 = agent_registry.get_instance("product_agent")
        assert a1 is a2

    def test_get_class_returns_subclass_of_smart_agent(self):
        from agent_system.agents import agent_registry
        for cls in agent_registry.all_classes():
            assert issubclass(cls, SmartAgent), f"{cls.__name__} not a SmartAgent subclass"

    def test_count_is_9(self):
        from agent_system.agents import agent_registry
        assert agent_registry.count() == 9

    def test_reset_then_reindex(self):
        """After reset(), only explicitly re-registered agents are present.
        We avoid importlib.reload() (pollutes other modules holding class refs);
        instead use the public register_agent() entry point.
        """
        from agent_system.agents import agent_registry
        from agent_system.core.registry import discover_agents, register_agent
        from agent_system.agents import ProductAgent
        # The fixture re-registered all 9 agents before this test.
        original_count = agent_registry.count()
        assert original_count == 9
        agent_registry.reset()
        assert agent_registry.count() == 0
        # Re-register a single agent via the public API.
        register_agent(ProductAgent)
        assert agent_registry.count() == 1
        # discover_agents without reloading is a no-op for already-imported modules.
        # It only fires @register_agent for modules that aren't yet imported.
        discover_agents()
        assert agent_registry.count() == 1  # only ProductAgent registered


class TestRegistryBackedPeerDiscovery:
    """_discover_peers() uses AgentRegistry instead of hardcoded list"""

    def test_discover_peers_excludes_self(self):
        from agent_system.agents.product_agent import ProductAgent
        from agent_system.core.evaluator import (
            ProblemAnalysis, ResolutionPath, Severity, ActionCategory,
        )
        from agent_system.core.resolver import SmartResolver
        agent = ProductAgent()
        resolver = SmartResolver(agent)
        analysis = ProblemAnalysis(
            severity=Severity.MEDIUM,
            confidence=0.5,
            can_self_solve=False,
            needs_peer_help=True,
            action_category=ActionCategory.NORMAL,
            suggested_path=ResolutionPath.PEER,
            reasoning="test",
            error_summary="TypeError in code implementation",
        )
        peers = resolver._discover_peers(analysis)
        peer_names = [n for n, _ in peers]
        assert "product_agent" not in peer_names

    def test_discover_peers_returns_3_top_scored(self):
        from agent_system.agents.test_agent import TestAgent
        from agent_system.core.evaluator import (
            ProblemAnalysis, ResolutionPath, Severity, ActionCategory,
        )
        from agent_system.core.resolver import SmartResolver
        agent = TestAgent()
        resolver = SmartResolver(agent)
        analysis = ProblemAnalysis(
            severity=Severity.MEDIUM,
            confidence=0.5,
            can_self_solve=False,
            needs_peer_help=True,
            action_category=ActionCategory.NORMAL,
            suggested_path=ResolutionPath.PEER,
            reasoning="test",
            error_summary="something",
        )
        peers = resolver._discover_peers(analysis)
        assert len(peers) == 3
        assert "test_agent" not in [n for n, _ in peers]

    def test_new_agent_appears_without_resolver_changes(self):
        """Adding a new @register_agent class shows up in _discover_peers without edits."""
        from agent_system.agents import agent_registry
        from agent_system.agents.product_agent import ProductAgent
        from agent_system.core.evaluator import (
            ProblemAnalysis, ResolutionPath, Severity, ActionCategory,
        )
        from agent_system.core.resolver import SmartResolver

        class _TempAgent(SmartAgent):
            agent_name: str = "_temp_test_agent_prb"
            agent_capabilities: list = ["ephemeral test"]
            description: str = "Temp"
            model_config = ConfigDict(extra="allow")

            async def do_work(self, task: TaskContext) -> OutputSchema:
                return OutputSchema(
                    id="tmp", type="result",
                    created_at=datetime.now(timezone.utc),
                    created_by=self.agent_name,
                    payload={},
                )

        try:
            agent_registry.register(_TempAgent)
            assert "_temp_test_agent_prb" in agent_registry.all_names()

            agent = ProductAgent()
            resolver = SmartResolver(agent)
            analysis = ProblemAnalysis(
                severity=Severity.MEDIUM,
                confidence=0.5,
                can_self_solve=False,
                needs_peer_help=True,
                action_category=ActionCategory.NORMAL,
                suggested_path=ResolutionPath.PEER,
                reasoning="test",
                error_summary="ephemeral test",
            )
            peers = resolver._discover_peers(analysis)
            peer_names = [n for n, _ in peers]
            assert "_temp_test_agent_prb" in peer_names
        finally:
            agent_registry.reset()
            from agent_system.core.registry import discover_agents
            discover_agents()


class TestRegistryBackedDiscussionAdapter:
    """_PeerDiscussionAdapter._setup_default_peers uses AgentRegistry"""

    def test_setup_default_peers_uses_registry(self):
        from agent_system.core.resolver import _PeerDiscussionAdapter
        from agent_system.agents.product_agent import ProductAgent
        adapter = _PeerDiscussionAdapter(ProductAgent())
        # After construction, default peers should include registry-discovered agents
        all_peers = adapter._all_peers()
        # Self (product_agent) must be excluded
        assert "product_agent" not in all_peers
        # All other registered agents should appear
        for name in ("tech_agent", "test_agent", "ceo_agent", "deploy_agent",
                     "devops_agent", "docs_agent", "review_agent", "security_agent"):
            assert name in all_peers, f"{name} not in default peers"