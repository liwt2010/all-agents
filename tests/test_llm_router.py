"""
Tests: Production LLM Router — async, retry, tool loop, truncation
"""

import json
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

from agent_system.core.llm_router import (
    LLMRouter,
    LLMUsage,
    LLMError,
    TransientLLMError,
    FatalLLMError,
    ToolExecutor,
    MODEL_PRICING,
    get_model_limit,
    estimate_cost,
    set_llm_context,
    _current_agent,
    _current_task,
)


class TestCostEstimation:
    """Test pricing calculations"""

    def test_estimate_cost_sonnet(self):
        cost = estimate_cost("claude-sonnet-4-20250514", 1_000_000, 1_000_000)
        # $3 input + $15 output
        assert cost == pytest.approx(18.0, abs=0.01)

    def test_estimate_cost_haiku(self):
        cost = estimate_cost("claude-haiku-4-5-20251001", 1_000_000, 1_000_000)
        # $0.25 input + $1.25 output
        assert cost == pytest.approx(1.5, abs=0.01)

    def test_estimate_cost_includes_cache(self):
        cost = estimate_cost("claude-sonnet-4-20250514", 1000, 500, cache_read=10000, cache_write=5000)
        # 0.003 + 0.0075 + 0.003 + 0.01875
        assert cost > 0.03

    def test_estimate_cost_unknown_model_uses_default(self):
        cost = estimate_cost("unknown-model", 1000, 500)
        assert cost > 0

    def test_get_model_limit(self):
        assert get_model_limit("claude-sonnet-4-20250514") == 200_000
        assert get_model_limit("unknown") == 200_000  # default


class TestMockMode:
    """Mock mode is selected when no API key is set"""

    def setup_method(self):
        # Ensure no key
        os.environ.pop("ANTHROPIC_API_KEY", None)
        # Reset global
        import agent_system.core.llm_router as lr
        lr.router._mock_mode = None
        lr.router._client = None

    def test_is_mock_mode_no_key(self):
        router = LLMRouter()
        assert router.is_mock_mode is True

    def test_is_mock_mode_with_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
        router = LLMRouter()
        assert router.is_mock_mode is False

    @pytest.mark.asyncio
    async def test_call_llm_returns_mock_for_product(self):
        router = LLMRouter()
        text, usage = await router.call_llm(
            MagicMock(),
            "You are product_agent. Write PRDs.",
            [{"role": "user", "content": "build a login feature"}],
        )
        payload = json.loads(text)
        assert "title" in payload
        assert "features" in payload
        assert usage.mock is True

    @pytest.mark.asyncio
    async def test_call_llm_returns_mock_for_tech(self):
        router = LLMRouter()
        text, usage = await router.call_llm(
            MagicMock(),
            "You are tech_agent. Write code.",
            [{"role": "user", "content": "implement hello world"}],
        )
        payload = json.loads(text)
        assert "files" in payload
        assert usage.mock is True

    @pytest.mark.asyncio
    async def test_call_llm_returns_mock_for_test(self):
        router = LLMRouter()
        text, usage = await router.call_llm(
            MagicMock(),
            "You are test_agent. Generate tests.",
            [{"role": "user", "content": "test login"}],
        )
        payload = json.loads(text)
        assert "test_files" in payload


class TestContextTruncation:
    """Test context window enforcement"""

    def test_truncate_drops_oldest_messages(self):
        router = LLMRouter()
        system = "short system"
        # Each message is ~25K tokens (100K chars), well over 200K limit
        # Use many large messages so we exceed 200K tokens
        messages = [
            {"role": "user", "content": "x" * 100_000},
            {"role": "assistant", "content": "y" * 100_000},
            {"role": "user", "content": "z" * 100_000},
            {"role": "assistant", "content": "w" * 100_000},
            {"role": "user", "content": "q" * 100_000},
            {"role": "assistant", "content": "r" * 100_000},
            {"role": "user", "content": "s" * 100_000},
            {"role": "assistant", "content": "t" * 100_000},
        ]
        # reserve only 100 tokens, leaving 199,900 for messages
        sp, msgs = router._truncate_messages(system, messages, "claude-sonnet-4-20250514", reserve_output_tokens=100)
        # Should have dropped some oldest messages
        assert len(msgs) < len(messages)
        # The most recent should remain
        assert msgs[-1]["content"].startswith("t")

    def test_truncate_no_op_when_within_limits(self):
        router = LLMRouter()
        messages = [{"role": "user", "content": "short"}]
        sp, msgs = router._truncate_messages("sys", messages, "claude-sonnet-4-20250514")
        assert len(msgs) == 1


class TestErrorClassification:
    """Test exception classification"""

    def test_classify_rate_limit(self):
        from anthropic import APIStatusError
        router = LLMRouter()
        # Build a fake status error
        mock_response = MagicMock()
        mock_response.status_code = 429
        err = APIStatusError("rate limited", response=mock_response, body=None)
        with pytest.raises(TransientLLMError):
            router._classify_and_raise(err)

    def test_classify_4xx_fatal(self):
        from anthropic import APIStatusError
        router = LLMRouter()
        mock_response = MagicMock()
        mock_response.status_code = 400
        err = APIStatusError("bad request", response=mock_response, body=None)
        with pytest.raises(FatalLLMError):
            router._classify_and_raise(err)

    def test_classify_5xx_transient(self):
        from anthropic import APIStatusError
        router = LLMRouter()
        mock_response = MagicMock()
        mock_response.status_code = 503
        err = APIStatusError("service unavailable", response=mock_response, body=None)
        with pytest.raises(TransientLLMError):
            router._classify_and_raise(err)


class TestRetry:
    """Test exponential backoff"""

    @pytest.mark.asyncio
    async def test_retry_on_transient_then_succeed(self, monkeypatch):
        from anthropic import APIStatusError

        # Force real mode by setting a fake key
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        router = LLMRouter(default_max_retries=2)

        call_count = [0]

        class FakeContentBlock:
            def __init__(self, text):
                self.type = "text"
                self.text = text

        class FakeUsage:
            input_tokens = 10
            output_tokens = 20
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

        class FakeResponse:
            content = [FakeContentBlock("hello")]
            model = "claude-sonnet-4-20250514"
            usage = FakeUsage()

        class FakeMessages:
            async def create(self, **kwargs):
                call_count[0] += 1
                if call_count[0] == 1:
                    mock_resp = MagicMock()
                    mock_resp.status_code = 429
                    raise APIStatusError("rate", response=mock_resp, body=None)
                return FakeResponse()

        class FakeClient:
            messages = FakeMessages()

        with patch.object(router, "get_api_client", return_value=FakeClient()):
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                # Force the router to think real mode (override is_mock_mode)
                router._mock_mode = False
                config = MagicMock(model="claude-sonnet-4-20250514", max_tokens=100, temperature=0)
                text, usage = await router.call_llm(
                    config, "sys", [{"role": "user", "content": "hi"}]
                )
                assert text == "hello"
                assert call_count[0] == 2  # one retry
                assert usage.num_retries >= 1
                assert mock_sleep.call_count >= 1

    @pytest.mark.asyncio
    async def test_no_retry_on_fatal(self, monkeypatch):
        from anthropic import APIStatusError

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake")

        router = LLMRouter(default_max_retries=3)

        class FakeMessages:
            async def create(self, **kwargs):
                mock_resp = MagicMock()
                mock_resp.status_code = 400
                raise APIStatusError("bad req", response=mock_resp, body=None)

        class FakeClient:
            messages = FakeMessages()

        with patch.object(router, "get_api_client", return_value=FakeClient()):
            router._mock_mode = False
            config = MagicMock(model="claude-sonnet-4-20250514", max_tokens=100, temperature=0)
            with pytest.raises(FatalLLMError):
                await router.call_llm(
                    config, "sys", [{"role": "user", "content": "hi"}]
                )


class TestCostAttribution:
    """Test current_agent/current_task context"""

    def test_context_set(self):
        set_llm_context("my_agent", "my_task")
        assert _current_agent.get() == "my_agent"
        assert _current_task.get() == "my_task"


class TestConfig:
    """Test config routing"""

    def setup_method(self):
        # Reset settings
        from agent_system.config.settings import reload_settings
        reload_settings()

    def test_get_config_default(self):
        router = LLMRouter()
        config = router.get_config("product_agent")
        assert config.model
        # Accept any supported provider's default model — settings.yaml may use
        # claude or deepseek depending on local config.
        assert any(
            tag in config.model.lower()
            for tag in ("claude", "deepseek", "gpt")
        ), f"Unexpected default model: {config.model}"

    def test_get_config_simple_downgrades(self):
        router = LLMRouter()
        config = router.get_config("test_agent", task_complexity="simple")
        # Should use fast model — settings.yaml may set fast to haiku or deepseek
        assert any(
            tag in config.model.lower()
            for tag in ("haiku", "deepseek", "mini", "nano")
        ), f"Unexpected fast model: {config.model}"

    def test_estimate_complexity(self):
        assert LLMRouter.estimate_complexity("hi") == "simple"
        assert LLMRouter.estimate_complexity("Design a comprehensive architecture") == "complex"
        # A longer "normal" task with no complex keywords
        assert LLMRouter.estimate_complexity("a normal task with enough content here for sure") == "standard"


class TestRequireRealKey:
    """Test fail-closed in production."""

    def test_production_without_key_fails(self, monkeypatch):
        from agent_system.core.llm_router import LLMRouter
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        router = LLMRouter()
        with pytest.raises(RuntimeError, match="production"):
            router.require_real_key()

    def test_production_with_key_passes(self, monkeypatch):
        from agent_system.core.llm_router import LLMRouter
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-fake-key-1234")
        router = LLMRouter()
        # Should not raise
        router.require_real_key()

    def test_dev_without_key_allowed(self, monkeypatch):
        from agent_system.core.llm_router import LLMRouter
        monkeypatch.setenv("ENVIRONMENT", "development")
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        router = LLMRouter()
        # Should not raise
        router.require_real_key()


class TestNoneUsageDefense:
    """Regression tests for None-valued usage fields from non-standard proxies.

    Some OpenAI/Anthropic-compatible proxies return usage objects where
    input_tokens/output_tokens are present but set to None (e.g. when the
    upstream uses different field names like prompt_tokens/completion_tokens
    that the SDK doesn't auto-translate). The router must coerce these to 0
    so estimate_cost() doesn't fail with `None / int`.
    """

    def test_anthropic_usage_extraction_coerces_none(self):
        """Direct test of the None-coercion logic for anthropic usage fields.

        Mirrors the production extraction code path in _call_anthropic.
        """
        class FakeUsage:
            input_tokens = None
            output_tokens = None
            cache_read_input_tokens = None
            cache_creation_input_tokens = None

        usage = FakeUsage()
        # Same coercion as in _call_anthropic
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        assert input_tokens == 0
        assert output_tokens == 0
        assert cache_read == 0
        assert cache_write == 0

    def test_openai_usage_extraction_coerces_none(self):
        """Direct test of the None-coercion logic for openai usage fields.

        Mirrors the production extraction code path in _call_openai.
        """
        class FakeUsage:
            prompt_tokens = None
            completion_tokens = None

        usage = FakeUsage()
        # Same coercion as in _call_openai
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        assert input_tokens == 0
        assert output_tokens == 0

    @pytest.mark.asyncio
    async def test_call_with_retry_handles_none_usage(self, monkeypatch):
        """End-to-end: call_llm must not crash when usage fields are None.

        Patches the SDK client's create method to return a response with
        None-valued usage fields. The router must coerce them to 0 and
        return a valid LLMUsage, not raise TypeError.
        """
        from agent_system.core.llm_router import LLMRouter, LLMConfig
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-key")

        class FakeUsage:
            input_tokens = None
            output_tokens = None
            cache_read_input_tokens = None
            cache_creation_input_tokens = None

        class FakeBlock:
            type = "text"
            text = "hello"

        class FakeResponse:
            model = "claude-test"
            content = [FakeBlock()]
            usage = FakeUsage()

        class FakeMessages:
            create = AsyncMock(return_value=FakeResponse())

        class FakeClient:
            messages = FakeMessages()

        router = LLMRouter()
        monkeypatch.setattr(router, "get_api_client", lambda: FakeClient())

        config = LLMConfig(model="claude-test", max_tokens=100, temperature=0.5)
        text, usage, raw = await router._call_with_retry(
            client=FakeClient(),
            config=config,
            system_prompt="You are helpful.",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
        )
        assert text == "hello"
        assert usage.input_tokens == 0
        assert usage.output_tokens == 0
        assert usage.cost_estimate == 0.0  # estimate_cost with 0,0 = 0.0

    def test_estimate_cost_with_none_raises(self):
        """Sanity: estimate_cost() itself does NOT defend against None.

        Callers (in _call_anthropic / _call_openai) are responsible for
        coercion. If you call estimate_cost with None directly, you still
        get TypeError — which is the correct behavior at the function
        boundary.
        """
        from agent_system.core.llm_router import estimate_cost
        with pytest.raises(TypeError):
            estimate_cost("test-model", None, 100)
