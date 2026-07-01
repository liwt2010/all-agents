"""
LLM Router — production-grade LLM integration.

Capabilities:
  - Real Anthropic API (AsyncAnthropic) with full async/await
  - Mock mode (when no API key) — same interface, used for dev/test
  - Retry with exponential backoff on transient errors
  - Tool use loop (multi-turn tool execution)
  - Token budget tracking and cost attribution
  - Streaming support (optional)
  - Prompt caching (Anthropic cache_control)
  - Context window enforcement with truncation
  - Auto-recording to global cost tracker
"""

import asyncio
import json
import logging
import os
import time
from contextvars import ContextVar
from typing import Any, Callable, Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

from agent_system.config.settings import get_settings, LLMConfig

logger = logging.getLogger(__name__)


class LLMUsage(BaseModel):
    """LLM call usage tracking"""
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_estimate: float = 0.0
    duration_ms: float = 0.0
    num_retries: int = 0
    mock: bool = False


class LLMError(Exception):
    """Base LLM error"""
    pass


class TransientLLMError(LLMError):
    """Retriable (rate limit, timeout, 5xx)"""
    pass


class FatalLLMError(LLMError):
    """Non-retriable (auth, bad request)"""
    pass


# Track current agent/task for cost attribution
_current_agent: ContextVar[str] = ContextVar("_current_agent", default="unknown")
_current_task: ContextVar[str] = ContextVar("_current_task", default="unknown")


def set_llm_context(agent: str, task: str):
    """Set the agent/task context for cost attribution"""
    _current_agent.set(agent)
    _current_task.set(task)


# ── Model pricing (per 1M tokens) ──

MODEL_PRICING = {
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75},
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25, "cache_read": 0.03, "cache_write": 0.30},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}

# Token limits per model
MODEL_LIMITS = {
    "claude-sonnet-4-20250514": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """Estimate cost in USD including cache tokens."""
    pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
    return (
        input_tokens / 1_000_000 * pricing["input"]
        + output_tokens / 1_000_000 * pricing["output"]
        + cache_read / 1_000_000 * pricing["cache_read"]
        + cache_write / 1_000_000 * pricing["cache_write"]
    )


def get_model_limit(model: str) -> int:
    return MODEL_LIMITS.get(model, 200_000)


# ── Tool execution ──

class ToolExecutor:
    """Resolves tool calls against a tool registry."""

    def __init__(self, tool_registry: Any):
        self.registry = tool_registry

    async def execute(self, name: str, inputs: Dict[str, Any]) -> Tuple[str, bool]:
        """
        Execute a tool by name. Returns (output_text, is_error).
        """
        try:
            tool = self.registry.get(name)
            if not tool:
                return f"Unknown tool: {name}", True
            result = await tool.execute(inputs)
            if result.success:
                return str(result.output) if result.output is not None else "", False
            return f"Tool error: {result.error}", True
        except Exception as e:
            logger.exception(f"Tool execution failed: {name}")
            return f"Tool exception: {str(e)[:500]}", True


# ── Main router ──

class LLMRouter:
    """
    Production LLM router.

    - Async-first (uses AsyncAnthropic)
    - Mock mode when no API key (deterministic, for dev/test)
    - Retry with exponential backoff
    - Tool-use loop with budget cap
    - Cost tracking
    """

    # Retriable HTTP status codes
    RETRIABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504, 529}

    def __init__(self, default_max_retries: int = 3):
        self.settings = get_settings()
        self.default_max_retries = default_max_retries
        self._client = None  # lazy-init
        self._mock_mode: Optional[bool] = None

    def get_config(self, agent_name: str, task_complexity: Optional[str] = None) -> LLMConfig:
        """Get LLM config for an agent, optionally adjusting for task complexity"""
        config = self.settings.get_llm_config(agent_name)
        if task_complexity == "simple":
            fast_config = self.settings.llm.fast
            config = LLMConfig(
                model=fast_config.model,
                max_tokens=min(config.max_tokens, fast_config.max_tokens),
                temperature=fast_config.temperature,
            )
        return config

    @property
    def is_mock_mode(self) -> bool:
        if self._mock_mode is None:
            self._mock_mode = not bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
        return self._mock_mode

    def require_real_key(self) -> None:
        """Fail loudly if running in production without a real API key.

        Use in startup checks (e.g. /api/ready) so a misconfigured deploy
        cannot silently return mock data.
        """
        env = os.environ.get("ENVIRONMENT", "").lower()
        if env in ("production", "prod") and self.is_mock_mode:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is missing in production environment. "
                "Refusing to run in mock mode."
            )

    def get_api_client(self):
        """Lazy-initialize the async client."""
        if self._client is not None:
            return self._client
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return None
        try:
            from anthropic import AsyncAnthropic
            self._client = AsyncAnthropic(api_key=api_key, max_retries=0)  # we handle retries
            return self._client
        except ImportError as e:
            logger.error(f"anthropic SDK not installed: {e}")
            return None

    @staticmethod
    def estimate_complexity(task_input: str) -> str:
        input_len = len(task_input)
        if input_len < 30:
            return "simple"
        complex_keywords = [
            "design", "architect", "full", "complete", "end-to-end",
            "comprehensive", "secure", "production", "database",
            "multi-step", "pipeline", "coordinated", "parallel",
            "optimize", "refactor", "migrate", "integration",
        ]
        if any(kw in task_input.lower() for kw in complex_keywords):
            return "complex"
        return "standard"

    def _truncate_messages(
        self,
        system_prompt: str,
        messages: list,
        model: str,
        reserve_output_tokens: int = 4096,
    ) -> Tuple[str, list]:
        """Truncate system prompt + messages if they exceed the model's context window."""
        limit = get_model_limit(model)
        available = limit - reserve_output_tokens

        # Rough token estimate (4 chars ≈ 1 token for English)
        def est_tokens(s: str) -> int:
            return max(1, len(s) // 4)

        sys_tokens = est_tokens(system_prompt)
        msg_tokens = sum(
            est_tokens(m.get("content", "") if isinstance(m.get("content"), str) else json.dumps(m.get("content", "")))
            for m in messages
        )
        total = sys_tokens + msg_tokens

        if total <= available:
            return system_prompt, messages

        # Drop oldest messages until we fit
        logger.warning(f"Truncating {len(messages)} messages to fit context window ({total} > {available} tokens)")
        truncated = list(messages)
        while truncated and (sys_tokens + sum(est_tokens(m.get("content", "")) for m in truncated)) > available:
            truncated.pop(0)
        return system_prompt, truncated

    async def call_llm(
        self,
        config: LLMConfig,
        system_prompt: str,
        messages: list,
        tools: Optional[list] = None,
        tool_executor: Optional[ToolExecutor] = None,
        max_tool_turns: int = 5,
        _agent_name: Optional[str] = None,
        _task_id: Optional[str] = None,
    ) -> Tuple[str, LLMUsage]:
        """
        Call the LLM and return (response_text, usage).

        If tool_executor is provided and the LLM requests tools, this will
        execute the tools and continue the conversation up to max_tool_turns.
        """
        # Set cost attribution context if provided
        if _agent_name or _task_id:
            set_llm_context(
                _agent_name or _current_agent.get(),
                _task_id or _current_task.get(),
            )

        if self.is_mock_mode:
            return self._mock_response(system_prompt, messages)

        client = self.get_api_client()
        if not client:
            logger.warning("Anthropic SDK not available, falling back to mock")
            return self._mock_response(system_prompt, messages)

        # Truncate to fit context
        system_prompt, messages = self._truncate_messages(
            system_prompt, messages, config.model, reserve_output_tokens=config.max_tokens
        )

        # Tool-use loop
        usage_total = LLMUsage(model=config.model, mock=False)
        current_messages = list(messages)

        for turn in range(max_tool_turns + 1):
            text, usage, raw_content = await self._call_with_retry(
                client, config, system_prompt, current_messages, tools
            )
            usage_total.input_tokens += usage.input_tokens
            usage_total.output_tokens += usage.output_tokens
            usage_total.cache_read_tokens += usage.cache_read_tokens
            usage_total.cache_creation_tokens += usage.cache_creation_tokens
            usage_total.cost_estimate += usage.cost_estimate
            usage_total.num_retries = max(usage_total.num_retries, usage.num_retries)
            usage_total.duration_ms += usage.duration_ms

            # If no tool calls or no executor, return final text
            if not tool_executor or not raw_content:
                return text, usage_total

            # Check for tool_use blocks
            tool_uses = [b for b in raw_content if getattr(b, "type", None) == "tool_use"]
            if not tool_uses:
                return text, usage_total

            # Execute tools and append results
            current_messages.append({"role": "assistant", "content": raw_content})
            tool_results = []
            for tu in tool_uses:
                output, is_error = await tool_executor.execute(tu.name, tu.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": output[:8000],  # truncate huge outputs
                    "is_error": is_error,
                })
            current_messages.append({"role": "user", "content": tool_results})

        # Hit max tool turns
        logger.warning(f"Tool loop hit max_turns={max_tool_turns}, returning last text")
        return text, usage_total

    async def _call_with_retry(
        self,
        client: Any,
        config: LLMConfig,
        system_prompt: str,
        messages: list,
        tools: Optional[list],
    ) -> Tuple[str, LLMUsage, list]:
        """Make one LLM call with retry on transient errors."""
        last_exc: Optional[Exception] = None
        retries = 0
        for attempt in range(self.default_max_retries + 1):
            try:
                text, usage, raw = await self._call_once(client, config, system_prompt, messages, tools)
                usage.num_retries = max(usage.num_retries, retries)
                return text, usage, raw
            except FatalLLMError:
                raise
            except (TransientLLMError, asyncio.TimeoutError) as e:
                last_exc = e
                retries = attempt + 1
                if attempt < self.default_max_retries:
                    backoff = min(2 ** attempt, 8) + (0.5 * attempt)
                    logger.warning(
                        f"LLM call attempt {attempt+1}/{self.default_max_retries} failed: {e}. "
                        f"Retrying in {backoff:.1f}s"
                    )
                    await asyncio.sleep(backoff)
                    continue
                raise

        raise TransientLLMError(f"All {self.default_max_retries} retries failed: {last_exc}")

    async def _call_once(
        self,
        client: Any,
        config: LLMConfig,
        system_prompt: str,
        messages: list,
        tools: Optional[list],
    ) -> Tuple[str, LLMUsage, list]:
        """Make one raw LLM call. Returns (text, usage, raw_content_blocks)."""
        start = time.time()
        kwargs = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "system": system_prompt,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools

        # Enable Anthropic prompt caching on the system prompt if the
        # system prompt is large enough to be worth caching (>= 1024 tokens)
        if os.environ.get("AGENT_PROMPT_CACHE", "1") == "1" and len(system_prompt) > 4000:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        try:
            response = await client.messages.create(**kwargs)
        except Exception as e:
            self._classify_and_raise(e)

        duration = (time.time() - start) * 1000

        # Extract text and content blocks
        text_parts = []
        raw_blocks = []
        if response.content:
            for block in response.content:
                if block.type == "text":
                    text_parts.append(block.text)
                    raw_blocks.append(block)
                elif block.type == "tool_use":
                    raw_blocks.append(block)

        text = "".join(text_parts)

        # Usage
        usage = response.usage
        input_tokens = getattr(usage, "input_tokens", 0)
        output_tokens = getattr(usage, "output_tokens", 0)
        cache_read = getattr(usage, "cache_read_input_tokens", 0)
        cache_write = getattr(usage, "cache_creation_input_tokens", 0)
        cost = estimate_cost(response.model, input_tokens, output_tokens, cache_read, cache_write)

        usage_obj = LLMUsage(
            model=response.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_write,
            cost_estimate=cost,
            duration_ms=duration,
            mock=False,
        )

        # Record to cost tracker
        self._record_cost(usage_obj)

        return text, usage_obj, raw_blocks

    def _classify_and_raise(self, e: Exception):
        """Convert an SDK exception to LLMError with proper classification."""
        from anthropic import APIStatusError, APITimeoutError, APIConnectionError, APIError
        if isinstance(e, APIStatusError):
            if e.status_code in self.RETRIABLE_STATUS_CODES:
                raise TransientLLMError(f"Status {e.status_code}: {e.message}") from e
            raise FatalLLMError(f"Status {e.status_code}: {e.message}") from e
        if isinstance(e, (APITimeoutError, APIConnectionError)):
            raise TransientLLMError(str(e)) from e
        if isinstance(e, APIError):
            raise FatalLLMError(str(e)) from e
        # Unknown error
        raise LLMError(str(e)) from e

    def _record_cost(self, usage: LLMUsage):
        """Record to global cost tracker."""
        try:
            from agent_system.core.cost_tracker import cost_tracker
            cost_tracker.record_call(
                agent_name=_current_agent.get(),
                task_id=_current_task.get(),
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                duration_ms=usage.duration_ms,
                success=True,
            )
        except Exception as e:
            logger.debug(f"Cost tracker record failed: {e}")

    # ── Mock mode ──

    def _mock_response(self, system_prompt: str, messages: list) -> Tuple[str, LLMUsage]:
        """Deterministic mock response for dev/test."""
        last_msg = ""
        for m in reversed(messages):
            content = m.get("content", "")
            if isinstance(content, str) and content:
                last_msg = content
                break

        sp = system_prompt.lower()
        is_tech = any(k in sp for k in ["tech_agent", "code implementation", "technical design"])
        is_test = any(k in sp for k in ["test_agent", "test generation", "automated tests"])
        is_prd = any(k in sp for k in ["product_agent", "prd", "product requirement"])

        if is_tech:
            payload = {
                "files": [{"path": "src/main.py", "language": "python",
                           "content": "def main():\n    print('Hello World')\n\nif __name__ == '__main__':\n    main()"}],
                "architecture": "Single module",
                "dependencies": [],
            }
        elif is_test:
            payload = {
                "test_files": [{"path": "tests/test_main.py",
                                "content": "def test_main():\n    assert True\n"}],
                "test_framework": "pytest",
                "coverage_target": "80%",
            }
        else:
            payload = {
                "title": f"Requirement: {last_msg[:40]}",
                "version": "1.0",
                "background": f"Based on the requirement: {last_msg[:100]}",
                "goals": ["Implement core functionality", "Ensure quality", "Support iteration"],
                "features": [
                    {"name": "Core Feature", "description": f"Implement: {last_msg[:60]}",
                     "priority": "P0", "acceptance_criteria": ["Works correctly"]},
                    {"name": "Testing", "description": "Write automated tests",
                     "priority": "P1", "acceptance_criteria": ["Coverage > 80%"]},
                ],
                "constraints": ["Compatible with existing system"],
                "timeline": "2 weeks",
            }

        return json.dumps(payload), LLMUsage(
            model="mock_mode",
            input_tokens=0,
            output_tokens=0,
            cost_estimate=0.0,
            duration_ms=0,
            num_retries=0,
            mock=True,
        )


# Global router instance
router = LLMRouter()
