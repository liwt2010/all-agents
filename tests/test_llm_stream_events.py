"""
Tests for the v0.4.0 streaming tool-call events.

`LLMRouter.stream_events()` yields a single channel of `StreamEvent`
dataclasses — text deltas plus tool_start / tool_input / tool_end
/ tool_result / done / error. The legacy `stream_chunks()` is
preserved as a text-only wrapper (see test_llm_stream.py).

These tests use fake async-stream fakes to feed both Anthropic
content_block_* events and OpenAI delta.tool_calls arrays through
the real _stream_*_events() coroutines.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import pytest

from agent_system.core.llm_router import (
    LLMConfig,
    LLMUsage,
    StreamEvent,
    router,
)


# ── Fake LLM event types ──


@dataclass
class FakeAnthropicTextDelta:
    type: str = "content_block_delta"

    def __init__(self, text: str):
        self.delta = type("_Delta", (), {"type": "text_delta", "text": text})()


@dataclass
class FakeAnthropicToolStart:
    type: str = "content_block_start"
    index: int = 0
    content_block: Any = None

    def __init__(self, idx: int, name: str, id_: str):
        self.index = idx
        self.content_block = type(
            "_Block", (), {"type": "tool_use", "name": name, "id": id_}
        )()


@dataclass
class FakeAnthropicToolInputDelta:
    type: str = "content_block_delta"
    index: int = 0

    def __init__(self, idx: int, partial: str):
        self.index = idx
        self.delta = type(
            "_Delta", (), {"type": "input_json_delta", "partial_json": partial}
        )()


@dataclass
class FakeAnthropicToolStop:
    type: str = "content_block_stop"
    index: int = 0

    def __init__(self, idx: int = 0):
        self.index = idx


@dataclass
class FakeAnthropicFinalUsage:
    input_tokens: int = 12
    output_tokens: int = 7
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


@dataclass
class FakeAnthropicFinalMessage:
    usage: Any = None

    def __init__(self, **kw):
        # Allow `usage=FakeAnthropicFinalUsage(...)` or set directly.
        self.usage = kw.get("usage") or FakeAnthropicFinalUsage(**kw)


class FakeAnthropicStream:
    """Async-iterable that mimics the Anthropic messages.stream context."""

    def __init__(self, events: list, final: FakeAnthropicFinalMessage):
        self._events = events
        self._final = final
        self._emitted = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def get_final_message(self):
        return self._final


# ── Mock + StreamEvent shape ──


class TestStreamEventShape:
    def test_event_dataclass_fields(self):
        ev = StreamEvent(kind="text", text="hi")
        assert ev.kind == "text"
        assert ev.text == "hi"
        assert ev.tool is None
        assert ev.delta is None
        assert ev.usage is None

    def test_event_with_tool_fields(self):
        ev = StreamEvent(
            kind="tool_start", tool="search", id="call_abc"
        )
        assert ev.kind == "tool_start"
        assert ev.tool == "search"
        assert ev.id == "call_abc"

    def test_event_with_input_delta(self):
        ev = StreamEvent(
            kind="tool_input", tool="search", id="x",
            delta='{"query":',
        )
        assert ev.delta == '{"query":'


# ── Mock-mode events ──


class TestMockStreamEvents:
    @pytest.mark.asyncio
    async def test_mock_yields_text_chunks_then_done(self):
        events: list[StreamEvent] = []
        async for ev in router.stream_events(
            LLMConfig(model="mock"), "system", [{"role": "user", "content": "x"}]
        ):
            events.append(ev)
        kinds = [e.kind for e in events]
        # Last event must be 'done'
        assert kinds[-1] == "done"
        # Multiple text events before done
        text_events = [e for e in events if e.kind == "text"]
        assert len(text_events) >= 2
        # Done carries LLMUsage
        done = events[-1]
        assert isinstance(done.usage, LLMUsage)
        assert done.usage.mock is True


# ── Anthropic events ──


class TestAnthropicEvents:
    @pytest.mark.asyncio
    async def test_anthropic_text_events(self, monkeypatch):
        """Mock the Anthropic client and verify event sequence for text-only."""
        events_seq = [
            FakeAnthropicTextDelta("Hello "),
            FakeAnthropicTextDelta("world"),
        ]
        final_msg = FakeAnthropicFinalMessage(usage=FakeAnthropicFinalUsage(
            input_tokens=10, output_tokens=5,
        ))

        class FakeStreamCtx:
            def __init__(self, **kw):
                self._stream = FakeAnthropicStream(events_seq, final_msg)

            async def __aenter__(self):
                return self._stream

            async def __aexit__(self, *a):
                return False

        class FakeAnthropicClient:
            def __init__(self, **kw):
                self.messages = type(
                    "_Msgs", (), {"stream": lambda self, **kw: FakeStreamCtx(**kw)}
                )()

        # Install the fake on the router
        monkeypatch.setattr(router, "_anthropic_client", FakeAnthropicClient())
        monkeypatch.setattr(router, "_mock_mode", False)
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")

        events: list[StreamEvent] = []
        async for ev in router.stream_events(
            LLMConfig(model="claude-haiku-4-5-20251001"),
            "system",
            [{"role": "user", "content": "x"}],
        ):
            events.append(ev)

        # Two text events + done
        kinds = [e.kind for e in events]
        assert kinds == ["text", "text", "done"]
        assert events[0].text == "Hello "
        assert events[1].text == "world"
        assert events[2].usage.input_tokens == 10

    @pytest.mark.asyncio
    async def test_anthropic_tool_call_event_sequence(self, monkeypatch):
        """Tool call: start → input (×N) → end → done."""
        events_seq = [
            FakeAnthropicTextDelta("Calling search... "),
            FakeAnthropicToolStart(idx=0, name="search", id_="call_001"),
            FakeAnthropicToolInputDelta(idx=0, partial='{"query":'),
            FakeAnthropicToolInputDelta(idx=0, partial='"hello"}'),
            FakeAnthropicToolStop(idx=0),
        ]
        final_msg = FakeAnthropicFinalMessage(usage=FakeAnthropicFinalUsage())

        class FakeStreamCtx:
            def __init__(self, **kw):
                self._stream = FakeAnthropicStream(events_seq, final_msg)
            async def __aenter__(self): return self._stream
            async def __aexit__(self, *a): return False

        class FakeAnthropicClient:
            def __init__(self, **kw):
                self.messages = type(
                    "_Msgs", (), {"stream": lambda self, **kw: FakeStreamCtx(**kw)}
                )()

        monkeypatch.setattr(router, "_anthropic_client", FakeAnthropicClient())
        monkeypatch.setattr(router, "_mock_mode", False)
        monkeypatch.setenv("LLM_PROVIDER", "anthropic")

        events: list[StreamEvent] = []
        async for ev in router.stream_events(
            LLMConfig(model="claude-haiku-4-5-20251001"),
            "system", [{"role": "user", "content": "x"}],
        ):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert kinds == [
            "text",         # "Calling search... "
            "tool_start",   # {tool: search, id: call_001}
            "tool_input",   # {"query":
            "tool_input",   # "hello"}
            "tool_end",     # JSON complete
            "done",
        ]
        # Tool events carry the right tool/id
        start, in1, in2, end = events[1], events[2], events[3], events[4]
        assert start.tool == "search" and start.id == "call_001"
        assert in1.tool == "search" and in1.id == "call_001"
        assert in1.delta == '{"query":'
        assert in2.delta == '"hello"}'
        assert end.tool == "search" and end.id == "call_001"


# ── OpenAI events ──


class TestOpenAIEvents:
    @pytest.mark.asyncio
    async def test_openai_text_events(self, monkeypatch):
        """Mock the OpenAI client and verify text-only events."""

        @dataclass
        class FakeChoice:
            class FakeDelta:
                def __init__(self, content=None, tool_calls=None):
                    self.content = content
                    self.tool_calls = tool_calls

            delta: Any = None

        @dataclass
        class FakeChunk:
            choices: list = None
            usage: Any = None

        chunks = [
            FakeChunk(choices=[FakeChoice(delta=FakeChoice.FakeDelta(content="Hi "))]),
            FakeChunk(choices=[FakeChoice(delta=FakeChoice.FakeDelta(content="there"))]),
            FakeChunk(choices=[]),
        ]

        class FakeStream:
            def __aiter__(self): return self
            async def __anext__(self):
                if not chunks:
                    raise StopAsyncIteration
                return chunks.pop(0)

        class FakeCompletions:
            async def create(self, **kw): return FakeStream()

        class FakeChat:
            completions = FakeCompletions()

        class FakeOpenAIClient:
            chat = FakeChat()

        monkeypatch.setattr(router, "_openai_client", FakeOpenAIClient())
        monkeypatch.setattr(router, "_mock_mode", False)
        monkeypatch.setenv("LLM_PROVIDER", "openai")

        events: list[StreamEvent] = []
        async for ev in router.stream_events(
            LLMConfig(model="deepseek-chat"),
            "system", [{"role": "user", "content": "x"}],
        ):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert kinds == ["text", "text", "done"]
        assert events[0].text == "Hi "
        assert events[1].text == "there"

    @pytest.mark.asyncio
    async def test_openai_tool_call_event_sequence(self, monkeypatch):
        """Tool call: start → input (×N) → end → done."""
        @dataclass
        class FakeFunction:
            name: str = ""
            arguments: str = ""

        @dataclass
        class FakeToolCall:
            index: int = 0
            id: str = ""
            function: Any = None
            type: str = "function"

        @dataclass
        class FakeDelta:
            content: Any = None
            tool_calls: Any = None

        @dataclass
        class FakeChoice:
            delta: Any = None

        @dataclass
        class FakeChunk:
            choices: list = None

        # First chunk: tool_start (id + name)
        tc1 = FakeToolCall(index=0, id="call_xyz", function=FakeFunction(name="lookup", arguments=""))
        # Subsequent chunks: tool_input with partial arguments
        tc2 = FakeToolCall(index=0, function=FakeFunction(arguments='{"q":'))
        tc3 = FakeToolCall(index=0, function=FakeFunction(arguments='"hi"}'))
        # Empty chunk to flush
        chunks = [
            FakeChunk(choices=[FakeChoice(delta=FakeDelta(tool_calls=[tc1]))]),
            FakeChunk(choices=[FakeChoice(delta=FakeDelta(tool_calls=[tc2]))]),
            FakeChunk(choices=[FakeChoice(delta=FakeDelta(tool_calls=[tc3]))]),
            FakeChunk(choices=[]),
        ]

        class FakeStream:
            def __aiter__(self): return self
            async def __anext__(self):
                if not chunks:
                    raise StopAsyncIteration
                return chunks.pop(0)

        class FakeCompletions:
            async def create(self, **kw): return FakeStream()

        class FakeChat:
            completions = FakeCompletions()

        class FakeOpenAIClient:
            chat = FakeChat()

        monkeypatch.setattr(router, "_openai_client", FakeOpenAIClient())
        monkeypatch.setattr(router, "_mock_mode", False)
        monkeypatch.setenv("LLM_PROVIDER", "openai")

        events: list[StreamEvent] = []
        async for ev in router.stream_events(
            LLMConfig(model="deepseek-chat"),
            "system", [{"role": "user", "content": "x"}],
        ):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert kinds == ["tool_start", "tool_input", "tool_input", "tool_end", "done"]
        # tool_start has tool=lookup, id=call_xyz
        assert events[0].tool == "lookup"
        assert events[0].id == "call_xyz"
        # tool_input deltas accumulate
        assert events[1].delta == '{"q":'
        assert events[2].delta == '"hi"}'
        # tool_end has no delta
        assert events[3].kind == "tool_end"
        assert events[3].tool == "lookup"
        assert events[3].id == "call_xyz"


# ── Backward compatibility: stream_chunks still works ──


class TestStreamChunksCompat:
    @pytest.mark.asyncio
    async def test_stream_chunks_yields_only_text_and_sentinel(self):
        """The legacy API must keep working — text deltas + final sentinel."""
        from collections import namedtuple
        chunks: list = []
        async for c in router.stream_chunks(
            LLMConfig(model="mock"), "system", [{"role": "user", "content": "x"}]
        ):
            chunks.append(c)
        # All except the last are str; last is StreamEnd
        assert all(isinstance(c, str) for c in chunks[:-1])
        last = chunks[-1]
        assert last.__class__.__name__ == "StreamEnd"
        assert hasattr(last, "usage")
        assert last.usage.mock is True
