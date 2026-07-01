"""
Tests: FTUE flow
"""

import pytest
from datetime import datetime, timezone

from agent_system.onboarding.ftue import (
    FTUEStep,
    FTUESampleTask,
    FTUEState,
    FTUEManager,
    DEFAULT_SAMPLES,
    get_ftue_manager,
)


class TestFTUESampleTask:
    def test_default_samples_present(self):
        assert len(DEFAULT_SAMPLES) >= 3
        titles = [s.title for s in DEFAULT_SAMPLES]
        assert any("description" in t.lower() for t in titles)

    def test_samples_have_required_fields(self):
        for s in DEFAULT_SAMPLES:
            assert s.id
            assert s.title
            assert s.sample_input
            assert s.expected_output


class TestFTUEState:
    def test_initial_state(self):
        s = FTUEState(user_id="u1")
        assert s.current_step == FTUEStep.AUTO_ACCOUNT
        assert s.completed_at is None
        assert s.time_to_value_seconds is None

    def test_ttv_calculation(self):
        s = FTUEState(user_id="u1")
        s.started_at = datetime.now(timezone.utc) - timedelta(seconds=20)
        s.completed_at = datetime.now(timezone.utc)
        ttv = s.time_to_value_seconds
        assert ttv is not None
        assert 19 <= ttv <= 21


from datetime import timedelta


class TestFTUEManager:
    def test_get_or_create(self):
        m = FTUEManager()
        s1 = m.get_or_create("u1")
        s2 = m.get_or_create("u1")
        assert s1 is s2

    def test_get_state_returns_none_for_unknown(self):
        m = FTUEManager()
        assert m.get_state("nobody") is None

    def test_advance(self):
        m = FTUEManager()
        s = m.advance("u1", FTUEStep.SHOW_PROGRESS)
        assert s.current_step == FTUEStep.SHOW_PROGRESS

    def test_complete_marks_timestamp(self):
        m = FTUEManager()
        m.advance("u1", FTUEStep.SHOW_PROGRESS)
        s = m.advance("u1", FTUEStep.SHOW_RESULT)
        assert s.completed_at is not None
        # Subsequent advance doesn't overwrite
        first = s.completed_at
        s2 = m.advance("u1", FTUEStep.CELEBRATE)
        assert s2.completed_at == first

    def test_record_choice(self):
        m = FTUEManager()
        s = m.record_choice("u1", "continue")
        assert s.choice == "continue"

    def test_get_samples(self):
        m = FTUEManager()
        samples = m.get_samples()
        assert len(samples) == 3
        # Order is preserved
        assert samples[0].id == "sample-1"

    def test_get_sample_by_id(self):
        m = FTUEManager()
        s = m.get_sample("sample-2")
        assert s is not None
        assert s.id == "sample-2"

    def test_get_sample_unknown(self):
        m = FTUEManager()
        assert m.get_sample("nope") is None

    def test_tenant_scoping(self):
        m = FTUEManager()
        s1 = m.get_or_create("u1", tenant_id="acme")
        s2 = m.get_or_create("u1", tenant_id="beta")
        assert s1 is not s2  # Different tenants
        assert s1.tenant_id == "acme"
        assert s2.tenant_id == "beta"

    def test_stats(self):
        m = FTUEManager()
        m.advance("u1", FTUEStep.SHOW_RESULT)
        m.advance("u2", FTUEStep.SHOW_PROGRESS)
        stats = m.stats()
        assert stats["total_users"] == 2
        assert stats["completed"] == 1
        assert stats["ftue_completion_rate"] == 0.5

    def test_stats_ttv_meets_target(self):
        m = FTUEManager()
        # Simulate a user completing in 10s
        s = m.advance("u1", FTUEStep.SHOW_PROGRESS)
        s.started_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        s.completed_at = datetime.now(timezone.utc)
        # Manually mark complete
        s2 = m.get_state("u1")
        s2.completed_at = s.completed_at
        stats = m.stats()
        # 10s < 30s target — should be counted
        assert stats["meets_ttv_target_pct"] == 1.0


class TestGlobalFTUE:
    def test_singleton(self):
        m1 = get_ftue_manager()
        m2 = get_ftue_manager()
        assert m1 is m2
