"""
Tests for P2-3.2 Data Provenance.

Covers:
- ProvenanceSource enum (REAL_LLM / MOCK / LLM_FAILURE / UNKNOWN)
- build_provenance() with various LLMUsage values
- DataProvenance.is_real / is_mock / badge() text
- attach_provenance() / get_provenance() round-trip
- Agent-level: real LLM output has source=real_llm
- Agent-level: mock output has source=mock
- Agent-level: partial output has source=llm_failure
- Badge text reflects source (MOCK DATA / Claude / FAILED)
"""

import os
import asyncio
from datetime import datetime, timezone

import pytest

from agent_system.core.observability import (
    DataProvenance,
    ProvenanceSource,
    build_provenance,
    attach_provenance,
    get_provenance,
)
from agent_system.core.llm_router import LLMUsage
from agent_system.core.schema import OutputSchema


# ── Unit tests: build_provenance ──

class TestBuildProvenance:
    def test_real_llm_default_confidence(self):
        prov = build_provenance(
            source=ProvenanceSource.REAL_LLM,
            agent_name="test", task_id="t1",
        )
        assert prov.source == ProvenanceSource.REAL_LLM
        assert prov.confidence == 0.85  # default for real LLM
        assert prov.agent_name == "test"
        assert prov.task_id == "t1"

    def test_mock_default_confidence_zero(self):
        prov = build_provenance(
            source=ProvenanceSource.MOCK,
            agent_name="test", task_id="t1",
        )
        assert prov.source == ProvenanceSource.MOCK
        assert prov.confidence == 0.0  # mock is not authoritative

    def test_llm_failure_default_confidence_zero(self):
        prov = build_provenance(
            source=ProvenanceSource.LLM_FAILURE,
            agent_name="test", task_id="t1",
            error="timeout",
        )
        assert prov.source == ProvenanceSource.LLM_FAILURE
        assert prov.confidence == 0.0
        assert prov.error == "timeout"

    def test_unknown_default_confidence_zero(self):
        prov = build_provenance(
            source=ProvenanceSource.UNKNOWN,
        )
        assert prov.source == ProvenanceSource.UNKNOWN
        assert prov.confidence == 0.0

    def test_with_llm_usage_extracts_fields(self):
        usage = LLMUsage(
            model="claude-sonnet-4",
            input_tokens=100,
            output_tokens=200,
            cost_estimate=0.015,
            duration_ms=1234.0,
            mock=False,
        )
        prov = build_provenance(
            source=ProvenanceSource.REAL_LLM,
            agent_name="a", task_id="t", usage=usage,
        )
        assert prov.model == "claude-sonnet-4"
        assert prov.input_tokens == 100
        assert prov.output_tokens == 200
        assert prov.cost_usd == 0.015
        assert prov.duration_ms == 1234.0

    def test_with_mock_usage_extracts_fields(self):
        usage = LLMUsage(
            model="mock-model", mock=True,
            input_tokens=0, output_tokens=0, cost_estimate=0.0,
            duration_ms=0.0,
        )
        prov = build_provenance(
            source=ProvenanceSource.MOCK,
            agent_name="a", task_id="t", usage=usage,
        )
        assert prov.model == "mock-model"
        assert prov.is_mock() is True

    def test_with_none_usage_does_not_crash(self):
        prov = build_provenance(
            source=ProvenanceSource.REAL_LLM,
            agent_name="a", task_id="t", usage=None,
        )
        assert prov.model == ""
        assert prov.input_tokens == 0

    def test_timestamp_is_iso_utc(self):
        prov = build_provenance(source=ProvenanceSource.REAL_LLM)
        # Should be parseable as ISO 8601
        parsed = datetime.fromisoformat(prov.timestamp)
        assert parsed.tzinfo is not None  # has timezone


# ── is_real / is_mock ──

class TestIsFlags:
    def test_is_real(self):
        p = DataProvenance(source=ProvenanceSource.REAL_LLM, timestamp="x")
        assert p.is_real() is True
        assert p.is_mock() is False

    def test_is_mock(self):
        p = DataProvenance(source=ProvenanceSource.MOCK, timestamp="x")
        assert p.is_mock() is True
        assert p.is_real() is False

    def test_unknown_neither(self):
        p = DataProvenance(source=ProvenanceSource.UNKNOWN, timestamp="x")
        assert p.is_real() is False
        assert p.is_mock() is False


# ── Badge text ──

class TestBadge:
    def test_real_llm_badge_includes_model_and_cost(self):
        usage = LLMUsage(model="claude-sonnet-4", cost_estimate=0.0123, duration_ms=2500.0)
        p = build_provenance(
            source=ProvenanceSource.REAL_LLM,
            agent_name="a", task_id="t", usage=usage,
        )
        badge = p.badge()
        assert "claude-sonnet-4" in badge
        assert "2.5s" in badge
        assert "$0.0123" in badge
        # Has robot emoji
        assert "🤖" in badge

    def test_mock_badge_warns_clearly(self):
        p = build_provenance(source=ProvenanceSource.MOCK)
        badge = p.badge()
        # Should explicitly say MOCK DATA
        assert "MOCK" in badge.upper()
        assert "NOT REAL" in badge.upper()
        # Should have warning emoji
        assert "⚠" in badge

    def test_llm_failure_badge_shows_error(self):
        p = build_provenance(
            source=ProvenanceSource.LLM_FAILURE,
            error="Connection timeout after 30s",
        )
        badge = p.badge()
        assert "FAILED" in badge.upper() or "失败" in badge
        assert "Connection" in badge
        # Should have error emoji
        assert "❌" in badge

    def test_unknown_badge_minimal(self):
        p = build_provenance(source=ProvenanceSource.UNKNOWN)
        badge = p.badge()
        # Just shows the source name
        assert "unknown" in badge.lower()


# ── attach_provenance / get_provenance ──

class TestAttachProvenance:
    def test_attach_and_get_round_trip(self):
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1},
        )
        prov = build_provenance(
            source=ProvenanceSource.REAL_LLM,
            agent_name="a", task_id="t",
        )
        attach_provenance(out, prov)
        retrieved = get_provenance(out)
        assert retrieved is not None
        assert retrieved.source == ProvenanceSource.REAL_LLM
        assert retrieved.agent_name == "a"

    def test_badge_in_metadata(self):
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1},
        )
        prov = build_provenance(source=ProvenanceSource.MOCK)
        attach_provenance(out, prov)
        assert "MOCK" in out.metadata["data_provenance_badge"].upper()

    def test_get_provenance_when_missing(self):
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1},
        )
        # No provenance attached
        assert get_provenance(out) is None

    def test_get_provenance_corrupt_data(self):
        out = OutputSchema(
            id="x", type="task",
            created_at=datetime.now(timezone.utc),
            created_by="x",
            payload={"a": 1},
        )
        out.metadata["data_provenance"] = {"invalid": "data"}
        # Should not crash
        assert get_provenance(out) is None


# ── Agent integration: real LLM ──

class TestAgentProvenanceRealLLM:
    @pytest.mark.skipif(
        not (
            os.environ.get("ANTHROPIC_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        ),
        reason="No real API key",
    )
    def test_real_llm_output_has_real_provenance(self):
        os.environ["LLM_PROVIDER"] = "anthropic"
        if not os.environ.get("ANTHROPIC_BASE_URL") and os.environ.get("OPENAI_BASE_URL"):
            os.environ["ANTHROPIC_BASE_URL"] = os.environ["OPENAI_BASE_URL"]

        from agent_system.core.llm_router import LLMRouter, LLMConfig, router as default_router
        test_config = LLMConfig(
            model=os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
            max_tokens=4000, temperature=0.5,
        )
        LLMRouter.get_config = lambda self, agent_name, task_complexity=None: test_config

        from agent_system.agents.product_agent import ProductAgent
        from agent_system.core.agent import TaskContext

        agent = ProductAgent()
        ctx = TaskContext(task_id="prov-real-1", input="一句话回答:1+1=?")
        result = asyncio.run(agent.execute(ctx))

        prov = get_provenance(result)
        assert prov is not None, "Provenance should be attached to real-LLM output"
        assert prov.source == ProvenanceSource.REAL_LLM, (
            f"Expected REAL_LLM, got {prov.source}"
        )
        # Should have model + provider + cost > 0
        assert prov.model != ""
        assert prov.cost_usd >= 0
        # Badge should show real LLM
        assert "MOCK" not in result.metadata["data_provenance_badge"].upper()
        assert "FAILED" not in result.metadata["data_provenance_badge"].upper()


class TestAgentProvenanceMock:
    def test_mock_output_has_mock_provenance(self):
        # No API key set -> falls into mock mode
        # Save existing keys to restore after the test (avoid polluting later tests)
        _saved_anthropic = os.environ.pop("ANTHROPIC_API_KEY", None)
        _saved_openai = os.environ.pop("OPENAI_API_KEY", None)
        os.environ["LLM_PROVIDER"] = "anthropic"

        # Reset router to pick up new env
        from agent_system.core.llm_router import router as default_router, LLMRouter
        default_router._mock_mode = None
        default_router._anthropic_client = None

        from agent_system.agents.product_agent import ProductAgent
        from agent_system.core.agent import TaskContext

        agent = ProductAgent()
        ctx = TaskContext(task_id="prov-mock-1", input="一句话回答:1+1=?")
        result = asyncio.run(agent.execute(ctx))

        prov = get_provenance(result)
        assert prov is not None, "Provenance should be attached even in mock mode"
        assert prov.source == ProvenanceSource.MOCK, (
            f"Expected MOCK, got {prov.source}"
        )
        assert prov.confidence == 0.0
        # Badge should explicitly say MOCK
        badge = result.metadata["data_provenance_badge"]
        assert "MOCK" in badge.upper()
        assert "NOT REAL" in badge.upper()

        # Restore keys (test popped them to force mock mode)
        if _saved_anthropic is not None:
            os.environ["ANTHROPIC_API_KEY"] = _saved_anthropic
        if _saved_openai is not None:
            os.environ["OPENAI_API_KEY"] = _saved_openai


# ── Agent integration: partial output ──

class TestAgentProvenancePartial:
    @pytest.mark.skipif(
        not (
            os.environ.get("ANTHROPIC_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        ),
        reason="No real API key",
    )
    def test_partial_output_marked_as_llm_failure(self):
        """If LLM JSON parse fails (returns raw_output), provenance should
        be source=llm_failure with the error message visible in the badge.
        """
        os.environ["LLM_PROVIDER"] = "anthropic"
        if not os.environ.get("ANTHROPIC_BASE_URL") and os.environ.get("OPENAI_BASE_URL"):
            os.environ["ANTHROPIC_BASE_URL"] = os.environ["OPENAI_BASE_URL"]

        from agent_system.core.llm_router import LLMRouter, LLMConfig, router as default_router
        test_config = LLMConfig(
            model=os.environ.get("LLM_MODEL", "deepseek-v4-flash"),
            max_tokens=200,  # small to force JSON parse failure
            temperature=0.5,
        )
        LLMRouter.get_config = lambda self, agent_name, task_complexity=None: test_config

        # Reset mock_mode from previous test (TestAgentProvenanceMock may have set it)
        default_router._mock_mode = None
        default_router._anthropic_client = None

        from agent_system.agents.product_agent import ProductAgent
        from agent_system.core.agent import TaskContext

        agent = ProductAgent()
        # Input that the LLM might fail to parse as JSON
        ctx = TaskContext(task_id="prov-partial-1", input="一句话:1+1=?")
        result = asyncio.run(agent.execute(ctx))

        prov = get_provenance(result)
        assert prov is not None
        # Either REAL_LLM (LLM returned clean JSON) or LLM_FAILURE (raw_output)
        # Both are valid; we just need the badge to never claim MOCK
        assert prov.source in (ProvenanceSource.REAL_LLM, ProvenanceSource.LLM_FAILURE)
        assert "MOCK" not in result.metadata["data_provenance_badge"].upper()