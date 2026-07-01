"""
Docs Agent — PLATFORM §6.4 (API docs, runbooks, ADRs)

Capabilities: API reference generation, runbook authoring, ADR
(Architectural Decision Records) generation, README updates,
changelog maintenance.
"""

import json
from datetime import datetime, timezone

from agent_system.core.agent import SmartAgent, TaskContext
from agent_system.core.schema import OutputSchema, NextStep


class DocsAgent(SmartAgent):

    agent_name: str = "docs_agent"
    agent_capabilities: list = [
        "API reference generation",
        "runbook authoring",
        "ADR generation",
        "README updates",
        "changelog maintenance",
        "tutorial writing",
    ]
    description: str = "Generates and maintains documentation, runbooks, and architectural decision records"

    async def do_work(self, task: TaskContext) -> OutputSchema:
        return OutputSchema(
            id=OutputSchema.generate_id("doc"),
            type="docs_result",
            created_at=datetime.now(timezone.utc),
            created_by=self.agent_name,
            schema_version="1.0",
            payload={
                "summary": f"Documentation for: {task.input[:200]}",
                "sections": [
                    {"heading": "Overview", "content": "Brief description of the topic"},
                    {"heading": "Prerequisites", "content": "List of required setup steps"},
                    {"heading": "Step-by-step", "content": "Numbered walkthrough"},
                    {"heading": "Troubleshooting", "content": "Common issues and fixes"},
                ],
                "code_examples": [],
                "next_review": "30 days",
            },
            metadata={"input": task.input[:300], "agent": self.agent_name},
            next_steps=[NextStep(action="review_docs", agent="human")],
        )
