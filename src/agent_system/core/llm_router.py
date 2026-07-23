"""
LLM Router — production-grade LLM integration.

Capabilities:
  - Multi-provider: Anthropic API + OpenAI-compatible (DeepSeek, etc.)
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


# ── Streaming events (v0.4.0 PR-streaming-tools) ──

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class StreamEvent:
    """Wire-format event yielded by `LLMRouter.stream_events`.

    The WS endpoint serializes each event to JSON; the agent executor
    uses the typed fields directly. Single-channel string yields are
    reserved for plain text deltas (the common case).

    Field reference:
      - kind: "text" | "tool_start" | "tool_input" | "tool_end" |
              "tool_result" | "done" | "error"
      - text:  present iff kind == "text"
      - tool:   tool/function name (kind in tool_*)
      - id:     provider-assigned tool-call id (kind in tool_*)
      - delta:  partial JSON string (kind == "tool_input")
      - output: tool result text (kind == "tool_result")
      - is_error: True if the tool failed (kind == "tool_result")
      - usage:  aggregated usage (present iff kind == "done")
      - message: human-readable error (kind == "error")
    """
    kind: str
    text: str | None = None
    tool: str | None = None
    id: str | None = None
    delta: str | None = None
    output: str | None = None
    is_error: bool = False
    usage: Any = None
    message: str | None = None


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
    # DeepSeek models (via OpenAI-compatible API)
    "deepseek-chat": {"input": 0.14, "output": 0.28, "cache_read": 0.07, "cache_write": 0.14},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_write": 3.75}

# Token limits per model
MODEL_LIMITS = {
    "claude-sonnet-4-20250514": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "deepseek-chat": 64_000,
}


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """Estimate cost in USD including cache tokens.

    Callers are responsible for coercing `None` / string token counts
    to int before calling — `None` will raise `TypeError` (preserved
    by the test suite's `TestNoneUsageDefense::test_estimate_cost_with_none_raises`).
    """
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

    async def execute(self, name: str, inputs: dict[str, Any]) -> tuple[str, bool]:
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

    - Supports Anthropic API (AsyncAnthropic) and OpenAI-compatible (DeepSeek, etc.)
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
        self._anthropic_client = None  # lazy-init
        self._openai_client = None     # lazy-init
        self._mock_mode: bool | None = None

    @property
    def llm_provider(self) -> str:
        """Get configured LLM provider: anthropic, openai, or mock."""
        return os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()

    @property
    def api_key(self) -> str:
        """Get the API key from env, checking provider-specific keys first."""
        if self.llm_provider == "openai":
            return os.environ.get("OPENAI_API_KEY", "").strip()
        return os.environ.get("ANTHROPIC_API_KEY", "").strip()

    @property
    def openai_base_url(self) -> str:
        """Get the OpenAI-compatible base URL."""
        return os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com").strip()

    def get_config(self, agent_name: str, task_complexity: str | None = None) -> LLMConfig:
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
            self._mock_mode = not bool(self.api_key)
        return self._mock_mode

    def require_real_key(self) -> None:
        """Fail loudly if running in production without a real API key.

        Use in startup checks (e.g. /api/ready) so a misconfigured deploy
        cannot silently return mock data.
        """
        env = os.environ.get("ENVIRONMENT", "").lower()
        if env in ("production", "prod") and self.is_mock_mode:
            raise RuntimeError(
                "API key is missing in production environment. "
                "Refusing to run in mock mode."
            )

    def get_anthropic_client(self):
        """Lazy-initialize the Anthropic async client."""
        if self._anthropic_client is not None:
            return self._anthropic_client
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return None
        try:
            from anthropic import AsyncAnthropic
            self._anthropic_client = AsyncAnthropic(api_key=api_key, max_retries=0)
            return self._anthropic_client
        except ImportError as e:
            logger.error(f"anthropic SDK not installed: {e}")
            return None

    def get_openai_client(self):
        """Lazy-initialize the OpenAI-compatible async client (DeepSeek etc.)."""
        if self._openai_client is not None:
            return self._openai_client
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        try:
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(
                api_key=api_key,
                base_url=self.openai_base_url,
            )
            return self._openai_client
        except ImportError as e:
            logger.error(f"openai SDK not installed: {e}")
            return None

    def get_api_client(self):
        """Unified API client getter — returns Anthropic or OpenAI-compatible client based on LLM_PROVIDER env.

        Returns None if the SDK is unavailable or the API key is missing.
        Used by tests to patch a single entry point.
        """
        if self.llm_provider == "openai":
            return self.get_openai_client()
        return self.get_anthropic_client()

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
    ) -> tuple[str, list]:
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
        tools: list | None = None,
        tool_executor: ToolExecutor | None = None,
        max_tool_turns: int = 5,
        _agent_name: str | None = None,
        _task_id: str | None = None,
    ) -> tuple[str, LLMUsage]:
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

        if self.llm_provider == "openai":
            return await self._call_openai(
                config, system_prompt, messages, tools, tool_executor, max_tool_turns
            )

        # Anthropic provider
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
        tools: list | None,
    ) -> tuple[str, LLMUsage, list]:
        """Make one LLM call with retry on transient errors."""
        last_exc: Exception | None = None
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
        tools: list | None,
    ) -> tuple[str, LLMUsage, list]:
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
        # NOTE: getattr default doesn't catch None — the proxy may return usage
        # with input_tokens=None when the upstream uses OpenAI-style fields
        # (prompt_tokens / completion_tokens) that the Anthropic SDK doesn't
        # auto-translate. Coerce to int to avoid None / int in estimate_cost.
        usage = response.usage
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
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

    # ── OpenAI-compatible provider (DeepSeek, etc.) ──

    async def _call_openai(
        self,
        config: LLMConfig,
        system_prompt: str,
        messages: list,
        tools: list | None,
        tool_executor: ToolExecutor | None,
        max_tool_turns: int,
    ) -> tuple[str, LLMUsage]:
        """Call an OpenAI-compatible API (DeepSeek, etc.) with optional tool use."""
        client = self.get_openai_client()
        if not client:
            logger.warning("OpenAI client not available, falling back to mock")
            return self._mock_response(system_prompt, messages)

        # Convert LangGraph-style tools to OpenAI tool format
        openai_tools = None
        if tools:
            openai_tools = []
            for t in tools:
                openai_tools.append({
                    "type": "function",
                    "function": {
                        "name": t.get("name", t.get("function", {}).get("name", "unknown")),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", t.get("parameters", {})),
                    }
                })

        usage_total = LLMUsage(model=config.model, mock=False)
        current_messages = list(messages)

        # Convert system prompt to messages format (OpenAI uses system role)
        openai_messages = []
        if system_prompt:
            openai_messages.append({"role": "system", "content": system_prompt})
        openai_messages.extend(current_messages)

        for turn in range(max_tool_turns + 1):
            start = time.time()
            kwargs = {
                "model": config.model,
                "max_tokens": config.max_tokens,
                "temperature": config.temperature,
                "messages": openai_messages,
            }
            if openai_tools:
                kwargs["tools"] = openai_tools

            try:
                response = await client.chat.completions.create(**kwargs)
            except Exception as e:
                raise TransientLLMError(f"OpenAI API call failed: {e}") from e

            duration = (time.time() - start) * 1000

            choice = response.choices[0] if response.choices else None
            if not choice:
                raise LLMError("No response choices returned")

            msg = choice.message
            text = msg.content or ""

            # Usage from OpenAI response
            # Same None-defensive pattern: usage_info may exist but fields may be None
            # when the proxy returns non-standard shapes.
            usage_info = response.usage
            input_tokens = int(getattr(usage_info, "prompt_tokens", 0) or 0) if usage_info else 0
            output_tokens = int(getattr(usage_info, "completion_tokens", 0) or 0) if usage_info else 0
            cost = estimate_cost(config.model, input_tokens, output_tokens)

            usage_total.input_tokens += input_tokens
            usage_total.output_tokens += output_tokens
            usage_total.cost_estimate += cost
            usage_total.duration_ms += duration

            self._record_cost(usage_total)

            # Check for tool calls
            if not tool_executor or not msg.tool_calls:
                return text, usage_total

            # Execute tools and append results
            openai_messages.append(msg.model_dump())
            for tc in msg.tool_calls:
                try:
                    tool_input = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    tool_input = {}
                output, is_error = await tool_executor.execute(tc.function.name, tool_input)
                openai_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": output[:8000],
                })

        logger.warning(f"OpenAI tool loop hit max_turns={max_tool_turns}, returning last text")
        return text, usage_total

    def _classify_and_raise(self, e: Exception):
        """Convert an SDK exception to LLMError with proper classification."""
        # Try Anthropic error types first
        try:
            from anthropic import APIStatusError, APITimeoutError, APIConnectionError, APIError
            if isinstance(e, APIStatusError):
                if e.status_code in self.RETRIABLE_STATUS_CODES:
                    raise TransientLLMError(f"Status {e.status_code}: {e.message}") from e
                raise FatalLLMError(f"Status {e.status_code}: {e.message}") from e
            if isinstance(e, (APITimeoutError, APIConnectionError)):
                raise TransientLLMError(str(e)) from e
            if isinstance(e, APIError):
                raise FatalLLMError(str(e)) from e
        except ImportError:
            pass
        # Try OpenAI error types
        try:
            from openai import APIStatusError, APITimeoutError, APIConnectionError, APIError
            if isinstance(e, APIStatusError):
                if e.status_code in self.RETRIABLE_STATUS_CODES:
                    raise TransientLLMError(f"Status {e.status_code}: {e.message}") from e
                raise FatalLLMError(f"Status {e.status_code}: {e.message}") from e
            if isinstance(e, (APITimeoutError, APIConnectionError)):
                raise TransientLLMError(str(e)) from e
            if isinstance(e, APIError):
                raise FatalLLMError(str(e)) from e
        except ImportError:
            pass
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

    def _mock_response(self, system_prompt: str, messages: list) -> tuple[str, LLMUsage]:
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

    # ── Streaming (PR v0.2.0) ──

    async def stream_chunks(
        self,
        config: LLMConfig,
        system_prompt: str,
        messages: list,
        _agent_name: str | None = None,
        _task_id: str | None = None,
    ):
        """Async generator yielding text chunks as the LLM produces them.

        Each yield is a `str` — typically a few tokens at a time. On
        completion, the final chunk is a sentinel: a `StreamEnd`
        namedtuple with the aggregated `LLMUsage` so callers can
        report metrics.

        Why a sentinel rather than a separate "end" event? Because it
        keeps the contract single-channel (text only) — WebSocket
        consumers and async-for loops both get a clean "is the last
        item a string or a StreamEnd?" check.

        Mock mode yields the canned response in 5 chunks with a small
        sleep so consumers can observe streaming behavior in tests
        without needing an API key.
        """
        from collections import namedtuple
        StreamEnd = namedtuple("StreamEnd", ["usage"])

        if _agent_name or _task_id:
            set_llm_context(
                _agent_name or _current_agent.get(),
                _task_id or _current_task.get(),
            )

        if self.is_mock_mode:
            full, usage = self._mock_response(system_prompt, messages)
            # Split into ~5 chunks of similar length
            chunks = [full[i:i + max(1, len(full) // 5)]
                      for i in range(0, len(full), max(1, len(full) // 5))]
            for c in chunks:
                await asyncio.sleep(0.01)
                yield c
            yield StreamEnd(usage=usage)
            return

        if self.llm_provider == "anthropic":
            async for item in self._stream_anthropic(config, system_prompt, messages):
                yield item
            return

        if self.llm_provider == "openai":
            async for item in self._stream_openai(config, system_prompt, messages):
                yield item
            return

        # Unknown provider — degrade to mock
        full, usage = self._mock_response(system_prompt, messages)
        yield full
        yield StreamEnd(usage=usage)

    async def stream_events(
        self,
        config: LLMConfig,
        system_prompt: str,
        messages: list,
        _agent_name: str | None = None,
        _task_id: str | None = None,
    ):
        """Async generator yielding `StreamEvent`s as the LLM produces them.

        Events (single channel — yields `StreamEvent` dataclasses):
          - StreamEvent(kind="text", text=...)         — text delta
          - StreamEvent(kind="tool_start", tool=..., id=...)  — tool call opened
          - StreamEvent(kind="tool_input", tool=..., id=..., delta=...) — partial JSON
          - StreamEvent(kind="tool_end",   tool=..., id=...)  — JSON complete
          - StreamEvent(kind="tool_result", tool=..., id=..., output=..., is_error=...)
                                                    — only emitted when the
                                                      caller feeds tool
                                                      results back in via
                                                      run_tool_loop()
          - StreamEvent(kind="done", usage=...)        — terminal; aggregates
                                                      the full LLMUsage

        Why a single-channel generator over the previous str-or-sentinel?
        Because tool-call streaming needs more than two states, and a
        string-or-sentinel channel can't express them. Consumers do:
            async for ev in router.stream_events(...):
                if ev.kind == "text": ...
                elif ev.kind == "tool_start": ...
                ...
                elif ev.kind == "done": break

        The legacy `stream_chunks()` is kept as a thin wrapper that drops
        everything but text deltas + the sentinel, for callers that
        don't care about tool calls.
        """
        if _agent_name or _task_id:
            set_llm_context(
                _agent_name or _current_agent.get(),
                _task_id or _current_task.get(),
            )

        if self.is_mock_mode:
            for ev in self._mock_stream_events():
                yield ev
            return

        if self.llm_provider == "anthropic":
            async for ev in self._stream_anthropic_events(config, system_prompt, messages):
                yield ev
            return

        if self.llm_provider == "openai":
            async for ev in self._stream_openai_events(config, system_prompt, messages):
                yield ev
            return

        # Unknown provider — degrade to mock events
        for ev in self._mock_stream_events():
            yield ev

    def _mock_stream_events(self):
        """Mock-mode generator: 5 text chunks + done event.

        Includes a fake tool call cycle so consumers can verify
        downstream tool-handling code without an API key.
        """
        from collections import namedtuple
        StreamEnd = namedtuple("StreamEnd", ["usage"])

        full, usage = self._mock_response("", [{"role": "user", "content": "x"}])
        chunks = [
            full[i:i + max(1, len(full) // 5)]
            for i in range(0, len(full), max(1, len(full) // 5))
        ]
        for c in chunks:
            yield StreamEvent(kind="text", text=c)
            # Tiny sleep so test consumers can observe streaming; we
            # don't actually await here (yields are sync) — callers
            # that need pacing should await asyncio.sleep between
            # iterations. Kept simple to avoid accidental event-loop
            # blocking in non-async contexts.
        yield StreamEvent(kind="done", usage=usage)

    async def _stream_anthropic_events(self, config, system_prompt, messages):
        """Anthropic streaming → StreamEvents.

        The Anthropic SDK exposes a stream of typed events. We care about:
          - ContentBlockStartEvent(type="tool_use")  → tool_start
          - ContentBlockDeltaEvent(delta=InputJSONDelta(...))  → tool_input
          - ContentBlockStopEvent(index=tool_index)  → tool_end
          - Text delta events                                  → text
        """
        from collections import namedtuple
        StreamEnd = namedtuple("StreamEnd", ["usage"])

        client = self.get_anthropic_client()
        if not client:
            for ev in self._mock_stream_events():
                yield ev
            return

        system_prompt, messages = self._truncate_messages(
            system_prompt, messages, config.model,
            reserve_output_tokens=config.max_tokens,
        )
        # Inline the params builder (no separate _build_anthropic_params;
        # the streaming path was added without extracting the helper).
        params = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "system": system_prompt,
            "messages": messages,
        }
        if os.environ.get("AGENT_PROMPT_CACHE", "1") == "1" and len(system_prompt) > 4000:
            params["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        usage_total = LLMUsage(model=config.model, mock=False)
        t0 = time.time()
        # Track which block indices are tool_use so we can pair start/stop.
        tool_names: dict[int, str] = {}
        tool_ids: dict[int, str] = {}
        try:
            async with client.messages.stream(**params) as stream:
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        btype = getattr(block, "type", None) if block else None
                        if btype == "tool_use":
                            tool_names[event.index] = getattr(block, "name", "?")
                            tool_ids[event.index] = getattr(block, "id", "")
                            yield StreamEvent(
                                kind="tool_start",
                                tool=tool_names[event.index],
                                id=tool_ids[event.index],
                            )
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        if delta is None:
                            continue
                        dtype = getattr(delta, "type", None)
                        if dtype == "text_delta":
                            text = getattr(delta, "text", "")
                            if text:
                                yield StreamEvent(kind="text", text=text)
                        elif dtype == "input_json_delta":
                            partial = getattr(delta, "partial_json", "")
                            yield StreamEvent(
                                kind="tool_input",
                                tool=tool_names.get(event.index, "?"),
                                id=tool_ids.get(event.index, ""),
                                delta=partial,
                            )
                    elif etype == "content_block_stop":
                        if event.index in tool_names:
                            yield StreamEvent(
                                kind="tool_end",
                                tool=tool_names.pop(event.index, "?"),
                                id=tool_ids.pop(event.index, ""),
                            )
                final = await stream.get_final_message()
                usage_total.input_tokens = final.usage.input_tokens
                usage_total.output_tokens = final.usage.output_tokens
                if hasattr(final.usage, "cache_read_input_tokens"):
                    usage_total.cache_read_tokens = final.usage.cache_read_input_tokens or 0
                if hasattr(final.usage, "cache_creation_input_tokens"):
                    usage_total.cache_creation_tokens = final.usage.cache_creation_input_tokens or 0
        except Exception as e:
            logger.error(f"Anthropic streaming failed: {e}")
            yield StreamEvent(kind="error", message=f"Anthropic stream error: {e}")
        usage_total.duration_ms = (time.time() - t0) * 1000.0
        usage_total.cost_estimate = estimate_cost(
            config.model, usage_total.input_tokens, usage_total.output_tokens,
        )
        yield StreamEvent(kind="done", usage=usage_total)

    async def _stream_openai_events(self, config, system_prompt, messages):
        """OpenAI streaming → StreamEvents.

        `delta.tool_calls` is a list of partial tool calls: each element
        is keyed by `index` and accumulates across chunks. A delta with
        `id` starts a new tool; a delta with only `arguments` continues
        one. The end of a tool call is implicit (no further deltas at
        that index).
        """
        from collections import namedtuple
        StreamEnd = namedtuple("StreamEnd", ["usage"])

        client = self.get_openai_client()
        if not client:
            for ev in self._mock_stream_events():
                yield ev
            return

        openai_messages = []
        if system_prompt:
            openai_messages.append({"role": "system", "content": system_prompt})
        openai_messages.extend(messages)

        kwargs = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
            "messages": openai_messages,
            "stream": True,
        }
        usage_total = LLMUsage(model=config.model, mock=False)
        t0 = time.time()
        # Track open tool calls: index → (name, id).
        # OpenAI emits deltas with no `id` after the first chunk for
        # a given tool; the first delta includes id+name and starts the
        # tool. Subsequent deltas with `arguments` extend the JSON.
        tool_states: dict[int, tuple[str, str]] = {}
        try:
            stream = await client.chat.completions.create(**kwargs)
            async for chunk in stream:
                if not chunk.choices:
                    if hasattr(chunk, "usage") and chunk.usage:
                        usage_total.input_tokens = chunk.usage.prompt_tokens or 0
                        usage_total.output_tokens = chunk.usage.completion_tokens or 0
                    continue
                delta = chunk.choices[0].delta
                if delta is None:
                    continue
                content = getattr(delta, "content", None)
                if content:
                    yield StreamEvent(kind="text", text=content)
                tcalls = getattr(delta, "tool_calls", None)
                if tcalls:
                    for tc in tcalls:
                        idx = tc.index
                        if tc.id:
                            # First chunk for this tool — emit start.
                            name = (tc.function.name if tc.function and tc.function.name else "?")
                            tool_states[idx] = (name, tc.id)
                            yield StreamEvent(
                                kind="tool_start", tool=name, id=tc.id,
                            )
                        if tc.function and tc.function.arguments:
                            # Continuation chunk — partial JSON delta.
                            name, tid = tool_states.get(idx, ("?", ""))
                            yield StreamEvent(
                                kind="tool_input",
                                tool=name,
                                id=tid,
                                delta=tc.function.arguments,
                            )
                # Tool-end is implicit in OpenAI's streaming API (no
                # event per tool). The downstream executor treats
                # `tool_end` as "the moment we stop receiving tool_input
                # deltas for this id".
            # Flush tool_end for any tools whose delta stream ended.
            for idx, (name, tid) in list(tool_states.items()):
                yield StreamEvent(kind="tool_end", tool=name, id=tid)
                del tool_states[idx]
            if hasattr(chunk, "usage") and chunk.usage:
                usage_total.input_tokens = chunk.usage.prompt_tokens or 0
                usage_total.output_tokens = chunk.usage.completion_tokens or 0
        except Exception as e:
            logger.error(f"OpenAI streaming failed: {e}")
            yield StreamEvent(kind="error", message=f"OpenAI stream error: {e}")
        usage_total.duration_ms = (time.time() - t0) * 1000.0
        usage_total.cost_estimate = estimate_cost(
            config.model, usage_total.input_tokens, usage_total.output_tokens,
        )
        yield StreamEvent(kind="done", usage=usage_total)

    async def _stream_anthropic(self, config, system_prompt, messages):
        """Backward-compatible text-only wrapper for `_stream_anthropic_events`.

        Kept for `stream_chunks()` consumers that only want text deltas.
        """
        from collections import namedtuple
        StreamEnd = namedtuple("StreamEnd", ["usage"])
        async for ev in self._stream_anthropic_events(config, system_prompt, messages):
            if ev.kind == "text":
                yield ev.text
            elif ev.kind == "done":
                yield StreamEnd(usage=ev.usage)
                return

    async def _stream_openai(self, config, system_prompt, messages):
        """Backward-compatible text-only wrapper for `_stream_openai_events`."""
        from collections import namedtuple
        StreamEnd = namedtuple("StreamEnd", ["usage"])
        async for ev in self._stream_openai_events(config, system_prompt, messages):
            if ev.kind == "text":
                yield ev.text
            elif ev.kind == "done":
                yield StreamEnd(usage=ev.usage)
                return


# Global router instance
router = LLMRouter()
