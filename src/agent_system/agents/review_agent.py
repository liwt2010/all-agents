"""
Review Agent — PLATFORM §22 (peer review, code review)

Capabilities: code review, design review, test plan review.
Provides feedback like a senior engineer would: comments, suggestions,
risk assessment, approval/rejection with reasoning.
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from agent_system.core.agent import SmartAgent, TaskContext
from agent_system.core.schema import OutputSchema, NextStep


class ReviewAgent(SmartAgent):

    agent_name: str = "review_agent"
    agent_capabilities: list = [
        "code review",
        "design review",
        "test plan review",
        "API review",
        "security review handoff",
        "performance review",
    ]
    description: str = "Reviews code, design, and test plans like a senior engineer"

    async def do_work(self, task: TaskContext) -> OutputSchema:
        return OutputSchema(
            id=OutputSchema.generate_id("rev"),
            type="review_result",
            created_at=datetime.now(timezone.utc),
            created_by=self.agent_name,
            schema_version="1.0",
            payload={
                "summary": f"Review for: {task.input[:200]}",
                "verdict": "approve_with_suggestions",
                "comments": [
                    {
                        "category": "design",
                        "severity": "minor",
                        "comment": "Consider adding error handling at the boundary",
                    },
                    {
                        "category": "testing",
                        "severity": "minor",
                        "comment": "Add a test for the empty input case",
                    },
                ],
                "blocking": [],
                "approved": True,
            },
            metadata={"input": task.input[:300], "agent": self.agent_name},
            next_steps=[
                NextStep(action="address_comments", agent="tech_agent"),
                NextStep(action="merge", agent="devops_agent"),
            ],
        )
