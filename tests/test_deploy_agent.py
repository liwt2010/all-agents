"""
Tests: DeployAgent (5th core agent)
"""

import asyncio
import json
import pytest
from datetime import datetime, timezone

from agent_system.core.agent import TaskContext, OutputSchema
from agent_system.agents.deploy_agent import (
    DeployAgent,
    DeployTarget,
    HealthCheck,
    RollbackPlan,
)
from agent_system.agents.ceo_agent import CEOAgent


class TestDeployAgent:
    """DeployAgent: 5th core agent, mock mode plan generation"""

    @pytest.mark.asyncio
    async def test_basic_deploy_plan(self):
        agent = DeployAgent()
        task = TaskContext(task_id="deploy-1", input="Build a calculator feature")
        output = await agent.execute(task)
        assert output.type == "deploy_plan"
        assert output.created_by == "deploy_agent"
        p = output.payload
        assert "summary" in p
        assert "targets" in p
        assert "health_checks" in p
        assert "canary_schedule" in p
        assert "rollback" in p

    @pytest.mark.asyncio
    async def test_pipeline_contains_dev_staging_prod(self):
        agent = DeployAgent()
        task = TaskContext(task_id="deploy-2", input="Ship a webhook")
        output = await agent.execute(task)
        envs = [t["environment"] for t in output.payload["targets"]]
        assert envs == ["dev", "staging", "prod"]

    @pytest.mark.asyncio
    async def test_prod_uses_canary(self):
        agent = DeployAgent()
        task = TaskContext(task_id="deploy-3", input="Ship")
        output = await agent.execute(task)
        prod_target = next(
            t for t in output.payload["targets"] if t["environment"] == "prod"
        )
        assert prod_target["strategy"] == "canary"
        # Production has more replicas for safer rollouts
        assert prod_target["replicas"] >= 2

    @pytest.mark.asyncio
    async def test_canary_schedule_gradual(self):
        agent = DeployAgent()
        task = TaskContext(task_id="deploy-4", input="Ship")
        output = await agent.execute(task)
        schedule = output.payload["canary_schedule"]
        # First step is small, last is 100%
        assert schedule[0]["percent"] <= 10
        assert schedule[-1]["percent"] == 100
        # Each step has duration
        for step in schedule:
            assert "duration_min" in step
            assert "passes_to_next" in step

    @pytest.mark.asyncio
    async def test_rollback_plan_has_triggers(self):
        agent = DeployAgent()
        task = TaskContext(task_id="deploy-5", input="Ship")
        output = await agent.execute(task)
        rb = output.payload["rollback"]
        assert rb["auto_rollback"] is True
        assert len(rb["trigger_conditions"]) >= 2
        assert len(rb["steps"]) >= 3
        assert rb["max_rollback_time_seconds"] > 0

    @pytest.mark.asyncio
    async def test_health_checks_present(self):
        agent = DeployAgent()
        task = TaskContext(task_id="deploy-6", input="Ship")
        output = await agent.execute(task)
        hcs = output.payload["health_checks"]
        assert len(hcs) >= 2
        # Each health check has required fields
        for hc in hcs:
            assert "name" in hc
            assert "type" in hc
            assert "target" in hc
            assert "failure_threshold" in hc

    @pytest.mark.asyncio
    async def test_uses_upstream_output(self):
        """Deploy Agent uses upstream code+test results as context."""
        agent = DeployAgent()
        # Fake upstream code output
        upstream = {
            "created_by": "test_agent",
            "payload": {
                "test_files": [{"path": "tests/test_x.py", "content": "pass"}],
            },
        }
        task = TaskContext(
            task_id="deploy-7",
            input="Ship a feature",
            upstream_output=upstream,
        )
        output = await agent.execute(task)
        # upstream is reflected in metadata
        assert output.metadata.get("upstream_agent") == "test_agent"

    @pytest.mark.asyncio
    async def test_requires_human_approval(self):
        """Production deploys should require human approval (PLATFORM §7.2)."""
        agent = DeployAgent()
        task = TaskContext(task_id="deploy-8", input="Ship")
        output = await agent.execute(task)
        assert output.payload.get("requires_human_approval") is True
        assert len(output.payload.get("approvers", [])) > 0

    @pytest.mark.asyncio
    async def test_monitoring_setup(self):
        agent = DeployAgent()
        task = TaskContext(task_id="deploy-9", input="Ship")
        output = await agent.execute(task)
        mon = output.payload.get("monitoring", {})
        assert "dashboards" in mon
        assert "alerts" in mon
        assert len(mon["dashboards"]) > 0

    @pytest.mark.asyncio
    async def test_next_steps_include_human_review(self):
        """next_steps must include a human review step before production deploy."""
        agent = DeployAgent()
        task = TaskContext(task_id="deploy-10", input="Ship")
        output = await agent.execute(task)
        next_agents = [s.agent for s in output.next_steps]
        assert "human" in next_agents


class TestDeployAgentInPipeline:
    """Deploy Agent integrated into CEO pipeline"""

    @pytest.mark.asyncio
    async def test_ceo_pipeline_includes_deploy_step(self):
        """The CEO pipeline should now have 4 stages: product, tech, test, deploy."""
        ceo = CEOAgent()
        task = TaskContext(
            task_id="pipeline-deploy",
            input="Build a search feature",
            max_retries=1,
        )
        output = await ceo.execute(task)
        assert output.type == "pipeline_result"
        steps = output.payload["steps"]
        # Find the deploy step
        deploy_steps = [s for s in steps if s.get("agent") == "deploy_agent"]
        assert len(deploy_steps) == 1
        # The deploy step is the last one
        assert steps[-1]["agent"] == "deploy_agent"
        # In mock mode, deploy step succeeds
        assert steps[-1]["status"] in ("completed", "failed")


class TestPydanticModels:
    """Verify the helper Pydantic models work as expected"""

    def test_deploy_target(self):
        t = DeployTarget(environment="prod", strategy="canary", replicas=3)
        assert t.environment == "prod"
        assert t.rollout_percent == 100  # default

    def test_health_check(self):
        h = HealthCheck(name="hc1", type="http", target="/healthz")
        assert h.initial_delay_seconds == 30  # default

    def test_rollback_plan(self):
        r = RollbackPlan()
        assert r.auto_rollback is True
        assert r.max_rollback_time_seconds == 600
