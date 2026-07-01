"""
Test Agent — generates and runs tests
"""

import json
from datetime import datetime, timezone
from typing import Any, Dict

from agent_system.core.agent import SmartAgent, TaskContext
from agent_system.core.schema import OutputSchema, NextStep


class TestAgent(SmartAgent):

    agent_name: str = "test_agent"
    agent_capabilities: list = [
        "test generation",
        "test execution",
        "coverage analysis",
        "bug reporting",
    ]
    description: str = "Responsible for generating and running automated tests"

    async def do_work(self, task: TaskContext) -> OutputSchema:
        router = self.llm_router
        config = router.get_config(self.agent_name, task_complexity="simple")

        # Use upstream code if available
        code_context = ""
        if task.upstream_output:
            code_context = f"\nUpstream Code: {json.dumps(task.upstream_output.get('payload', {}), ensure_ascii=False, indent=2)}"

        system_prompt = self.get_system_prompt() + """

## Test Output Format

Generate a JSON response with:
- test_framework: pytest / unittest / etc
- test_files: list of test files (each with path, content)
- test_commands: commands to run the tests
- coverage_target: coverage percentage target

Output ONLY valid JSON, no markdown code blocks.
"""

        messages = [
            {"role": "user", "content": f"Please generate tests for the following code:{code_context}\n\nTask description: {task.input}"}
        ]

        text, usage = await router.call_llm(config, system_prompt, messages, _agent_name=self.agent_name, _task_id=task.task_id)

        payload = self._parse_json(text)
        self._last_usage = usage

        return OutputSchema(
            id=OutputSchema.generate_id("test"),
            type="test_report",
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
                NextStep(action="review_results", agent="ceo_agent", description="Final review"),
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
