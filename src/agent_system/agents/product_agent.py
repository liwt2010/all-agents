"""
Product Agent — writes PRD requirements documents
Uses LLM Router for API calls (mock mode works without API key)
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from agent_system.core.registry import register_agent
from agent_system.core.agent import SmartAgent, TaskContext
from agent_system.core.schema import OutputSchema, NextStep
from agent_system.core.llm_router import router as default_router


@register_agent
class ProductAgent(SmartAgent):

    agent_name: str = "product_agent"
    agent_capabilities: list = [
        "requirement analysis",
        "PRD writing",
        "feature breakdown",
        "priority sorting",
        "acceptance criteria definition",
    ]
    description: str = "Responsible for writing and analyzing Product Requirement Documents (PRD)"

    async def do_work(self, task: TaskContext) -> OutputSchema:
        router = self.llm_router
        config = router.get_config(self.agent_name)

        system_prompt = self.get_system_prompt() + """

## PRD Output Format

Generate a JSON PRD with these fields:
- title: requirement title
- version: version number
- background: context description
- goals: list of goals
- features: list of features (each with name, description, priority, acceptance_criteria)
- constraints: list of constraints
- timeline: time estimate

Output ONLY valid JSON, no markdown code blocks.
"""

        messages = [
            {"role": "user", "content": f"Please write a PRD based on this requirement:\n\n{task.input}"}
        ]

        text, usage = await router.call_llm(config, system_prompt, messages, _agent_name=self.agent_name, _task_id=task.task_id)

        # Parse JSON from response
        payload = self._parse_json(text)

        self._last_usage = usage

        return OutputSchema(
            id=OutputSchema.generate_id("prd"),
            type="requirement",
            created_at=datetime.now(timezone.utc),
            created_by=self.agent_name,
            schema_version="1.0",
            payload=payload,
            metadata={
                "input": task.input,
                "model": usage.model,
                "usage": {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens},
            },
            next_steps=[
                NextStep(action="tech_estimate", agent="tech_agent", description="Technical estimation"),
            ],
        )

    def _parse_json(self, text: str) -> dict:
        """Parse LLM response text into a JSON dict"""
        text = text.strip()
        # Strip markdown code blocks
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0] if "```" in text else text
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw_output": text[:2000]}
