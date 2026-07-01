"""
Tech Agent — generates code from PRD
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict

from agent_system.core.agent import SmartAgent, TaskContext
from agent_system.core.schema import OutputSchema, NextStep


class TechAgent(SmartAgent):

    agent_name: str = "tech_agent"
    agent_capabilities: list = [
        "code generation",
        "architecture design",
        "technical estimation",
        "code review",
        "dependency management",
    ]
    description: str = "Responsible for technical design, estimation, and code implementation"

    async def do_work(self, task: TaskContext) -> OutputSchema:
        router = self.llm_router
        config = router.get_config(self.agent_name)

        # Use upstream PRD if available
        prd_context = ""
        if task.upstream_output:
            prd_context = f"\nUpstream PRD: {json.dumps(task.upstream_output.get('payload', {}), ensure_ascii=False, indent=2)}"

        system_prompt = self.get_system_prompt() + """

## Code Output Format

Generate a JSON response with:
- architecture: brief architecture description
- files: list of files to create (each with path, language, content)
- dependencies: list of dependencies
- setup_instructions: how to set up and run

Output ONLY valid JSON, no markdown code blocks.
"""

        messages = [
            {"role": "user", "content": f"Please implement code for the following requirement:{prd_context}\n\nTask description: {task.input}"}
        ]

        text, usage = await router.call_llm(config, system_prompt, messages, _agent_name=self.agent_name, _task_id=task.task_id)

        payload = self._parse_json(text)
        self._last_usage = usage

        return OutputSchema(
            id=OutputSchema.generate_id("code"),
            type="code",
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
                NextStep(action="generate_tests", agent="test_agent", description="Generate and run tests"),
            ],
        )

    def _parse_json(self, text: str) -> dict:
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            text = text.rsplit("```", 1)[0] if "```" in text else text
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw_output": text[:2000]}
