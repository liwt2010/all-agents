"""
DevOps Agent — PLATFORM §5.1 (Deploy/Operations)

Capabilities: deploy pipelines, monitor services, manage K8s,
CI/CD configuration, infrastructure-as-code review.

Pipeline stage: after Deploy (or alongside Deploy when no separate
Deploy agent is present).
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import Field

from agent_system.core.registry import register_agent
from agent_system.core.agent import SmartAgent, TaskContext
from agent_system.core.schema import OutputSchema, NextStep


@register_agent
class DevOpsAgent(SmartAgent):

    agent_name: str = "devops_agent"
    agent_capabilities: list = [
        "CI/CD pipeline setup",
        "K8s deployment",
        "monitoring + alerting",
        "infrastructure-as-code",
        "log analysis",
        "rollback execution",
    ]
    description: str = "Handles deployments, infrastructure, monitoring, and on-call operations"

    async def do_work(self, task: TaskContext) -> OutputSchema:
        return OutputSchema(
            id=OutputSchema.generate_id("devops"),
            type="devops_result",
            created_at=datetime.now(timezone.utc),
            created_by=self.agent_name,
            schema_version="1.0",
            payload={
                "summary": f"DevOps plan for: {task.input[:200]}",
                "actions": [
                    "Verify CI pipeline is green",
                    "Build container image",
                    "Tag image with semver + git SHA",
                    "Run canary deploy to 5% of pods",
                    "Monitor error rate for 10 minutes",
                    "Promote to 50% if healthy",
                    "Full rollout after another 10 minutes",
                ],
                "rollback": "kubectl rollout undo <deployment>",
                "monitoring": ["error_rate", "p99_latency", "cpu_usage"],
            },
            metadata={
                "input": task.input[:300],
                "agent": self.agent_name,
            },
            next_steps=[
                NextStep(action="execute_deploy", agent="devops_agent"),
                NextStep(action="verify", agent="devops_agent"),
            ],
        )
