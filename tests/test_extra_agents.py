"""
Tests: DevOps / Security / Docs / Review Agents
"""

import pytest
from datetime import datetime, timezone

from agent_system.core.agent import TaskContext
from agent_system.agents.devops_agent import DevOpsAgent
from agent_system.agents.security_agent import SecurityAgent
from agent_system.agents.docs_agent import DocsAgent
from agent_system.agents.review_agent import ReviewAgent


class TestDevOpsAgent:
    @pytest.mark.asyncio
    async def test_basic_run(self):
        agent = DevOpsAgent()
        task = TaskContext(task_id="dev-1", input="Deploy a new API service")
        output = await agent.execute(task)
        assert output.type == "devops_result"
        assert output.metadata["agent"] == "devops_agent"
        assert "actions" in output.payload
        assert "rollback" in output.payload

    def test_capabilities(self):
        agent = DevOpsAgent()
        assert any("CI/CD" in c for c in agent.agent_capabilities)


class TestSecurityAgent:
    @pytest.mark.asyncio
    async def test_basic_run(self):
        agent = SecurityAgent()
        task = TaskContext(task_id="sec-1", input="Review this code")
        output = await agent.execute(task)
        assert output.type == "security_result"
        assert "findings" in output.payload
        assert "recommendations" in output.payload

    def test_capabilities(self):
        agent = SecurityAgent()
        assert any("secrets" in c.lower() for c in agent.agent_capabilities)


class TestDocsAgent:
    @pytest.mark.asyncio
    async def test_basic_run(self):
        agent = DocsAgent()
        task = TaskContext(task_id="doc-1", input="Document the auth API")
        output = await agent.execute(task)
        assert output.type == "docs_result"
        assert "sections" in output.payload

    def test_capabilities(self):
        agent = DocsAgent()
        assert any("API" in c for c in agent.agent_capabilities)


class TestReviewAgent:
    @pytest.mark.asyncio
    async def test_basic_run(self):
        agent = ReviewAgent()
        task = TaskContext(task_id="rev-1", input="Review this PR")
        output = await agent.execute(task)
        assert output.type == "review_result"
        assert output.payload["approved"] is True
        assert "comments" in output.payload

    def test_capabilities(self):
        agent = ReviewAgent()
        assert any("review" in c.lower() for c in agent.agent_capabilities)
