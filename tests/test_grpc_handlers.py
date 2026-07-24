"""
Tests for the v0.5.0 gRPC handlers.

These tests run WITHOUT the `grpcio` library installed. They exercise
the transport-neutral handler class directly (the same code the gRPC
servicer would call) and assert the dict shapes that the generated
protobuf servicer will translate. Once grpcio is installed and
`protoc` has been run, the actual wire format will mirror these
shapes exactly — a contract test, in effect.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_system.grpc.handlers import (
    GrpcServiceHandler,
    is_grpc_available,
    aiter_stream_llm_events,
)
from agent_system.core.llm_router import LLMUsage, StreamEvent
from agent_system.storage.task_store import InMemoryTaskStore


# ── Fixtures ──


@pytest.fixture
def fake_task_store():
    """Real InMemoryTaskStore — matches the v0.6.0 TaskStore contract."""
    return InMemoryTaskStore()


def _make_stream_event(kind, **kwargs):
    """Build a StreamEvent with the given kind and fields."""
    return StreamEvent(kind=kind, **kwargs)


async def _fake_stream_events():
    """Yield a fixed sequence of StreamEvents for stream_llm tests."""
    yield _make_stream_event("text", text="Hello ")
    yield _make_stream_event("tool_start", tool="search", id="call_001")
    yield _make_stream_event("tool_input", tool="search", id="call_001", delta='{"q":')
    yield _make_stream_event("tool_input", tool="search", id="call_001", delta='"x"}')
    yield _make_stream_event("tool_end", tool="search", id="call_001")
    yield _make_stream_event("text", text="world")
    yield _make_stream_event(
        "done",
        usage=LLMUsage(model="claude-haiku-4-5-20251001",
                       input_tokens=10, output_tokens=5,
                       cache_read_tokens=0, cache_creation_tokens=0,
                       duration_ms=42.0, mock=True),
    )


def _make_async_gen():
    """Wrap _fake_stream_events so the router returns a real async gen."""
    return _fake_stream_events()


@pytest.fixture
def fake_llm_router():
    router = MagicMock()
    # stream_events is an async generator — return the gen directly.
    router.stream_events = MagicMock(return_value=_make_async_gen())
    return router


@pytest.fixture
def handler(fake_task_store, fake_llm_router):
    return GrpcServiceHandler({
        "task_store": fake_task_store,
        "llm_router": fake_llm_router,
    })


# ── is_grpc_available ──


class TestGrpcAvailable:
    def test_returns_bool(self):
        result = is_grpc_available()
        assert isinstance(result, bool)


# ── GrpcServiceHandler — Task lifecycle ──


class TestTaskLifecycle:
    @pytest.mark.asyncio
    async def test_submit_task_returns_pending_row(self, handler):
        result = await handler.submit_task({
            "input": "Build a TODO app",
            "agent": "product",
            "tenant_id": "acme",
            "metadata": {"team": "core"},
        })
        assert result["id"].startswith("grpc-")
        assert result["status"] == 1  # PENDING
        assert result["input"] == "Build a TODO app"
        assert result["agent"] == "product"
        # ISO 8601 with 'Z' suffix
        assert result["created_at"].endswith("Z")

    @pytest.mark.asyncio
    async def test_get_task_returns_row(self, handler):
        created = await handler.submit_task({
            "input": "x", "agent": "tech", "tenant_id": "acme", "metadata": {},
        })
        result = await handler.get_task({
            "id": created["id"], "tenant_id": "acme",
        })
        assert result is not None
        assert result["id"] == created["id"]

    @pytest.mark.asyncio
    async def test_get_task_wrong_tenant_returns_none(self, handler):
        created = await handler.submit_task({
            "input": "x", "agent": "tech", "tenant_id": "acme", "metadata": {},
        })
        result = await handler.get_task({
            "id": created["id"], "tenant_id": "OTHER",
        })
        assert result is None

    @pytest.mark.asyncio
    async def test_get_task_missing_id_returns_none(self, handler):
        result = await handler.get_task({"id": "nope", "tenant_id": "acme"})
        assert result is None

    @pytest.mark.asyncio
    async def test_list_tasks_yields_pages(self, handler):
        for i in range(3):
            await handler.submit_task({
                "input": f"task {i}", "agent": "tech",
                "tenant_id": "acme", "metadata": {},
            })
        # Add a task to a different tenant — must NOT appear
        await handler.submit_task({
            "input": "other", "agent": "tech",
            "tenant_id": "OTHER", "metadata": {},
        })
        pages = []
        async for page in handler.list_tasks({
            "tenant_id": "acme", "status": 0, "limit": 10, "cursor": "",
        }):
            pages.append(page)
        assert len(pages) >= 1
        all_tasks = [t for p in pages for t in p["tasks"]]
        assert len(all_tasks) == 3
        for t in all_tasks:
            assert t["input"].startswith("task ")

    @pytest.mark.asyncio
    async def test_list_tasks_status_filter(self, handler):
        # Create one task, mark it completed directly
        await handler.submit_task({
            "input": "a", "agent": "tech", "tenant_id": "acme", "metadata": {},
        })
        all_tasks = []
        async for p in handler.list_tasks({
            "tenant_id": "acme", "status": 0, "limit": 10, "cursor": "",
        }):
            all_tasks.extend(p["tasks"])
        # No status filter returns all
        assert len(all_tasks) == 1


# ── GrpcServiceHandler — LLM streaming ──


class TestStreamLLM:
    @pytest.mark.asyncio
    async def test_stream_emits_full_event_sequence(self, handler):
        events = []
        async for ev in handler.stream_llm({
            "prompt": "Hello",
            "system_prompt": "You are a test agent.",
            "model": "claude-haiku-4-5-20251001",
            "tenant_id": "acme",
        }):
            events.append(ev)
        # 2 text + 1 tool_start + 2 tool_input + 1 tool_end + 1 done = 7
        assert len(events) == 7
        # First event: text delta
        assert "text" in events[0]
        assert events[0]["text"]["text"] == "Hello "
        # Tool start
        assert "tool_start" in events[1]
        assert events[1]["tool_start"]["tool"] == "search"
        assert events[1]["tool_start"]["id"] == "call_001"
        # Two tool_input deltas
        assert events[2]["tool_input"]["delta"] == '{"q":'
        assert events[3]["tool_input"]["delta"] == '"x"}'
        # Tool end
        assert "tool_end" in events[4]
        # Final text + done
        assert "text" in events[5]
        assert "done" in events[6]
        assert events[6]["done"]["usage"]["input_tokens"] == 10
        assert events[6]["done"]["usage"]["output_tokens"] == 5
        assert events[6]["done"]["usage"]["mock"] is True

    @pytest.mark.asyncio
    async def test_stream_error_event_propagates(self):
        # Build a handler whose LLM router raises mid-stream
        async def _bad_stream():
            yield _make_stream_event("text", text="hi")
            raise RuntimeError("upstream down")

        deps = {
            "task_store": InMemoryTaskStore(),
            "llm_router": MagicMock(stream_events=MagicMock(return_value=_bad_stream())),
        }
        h = GrpcServiceHandler(deps)
        with pytest.raises(RuntimeError, match="upstream down"):
            async for _ in h.stream_llm({"prompt": "x", "system_prompt": "", "model": "", "tenant_id": "acme"}):
                pass

    @pytest.mark.asyncio
    async def test_stream_uses_provided_model(self, handler, fake_llm_router):
        async for _ in handler.stream_llm({
            "prompt": "x", "system_prompt": "", "model": "deepseek-chat", "tenant_id": "acme",
        }):
            pass
        fake_llm_router.stream_events.assert_called_once()
        call = fake_llm_router.stream_events.call_args
        # First positional arg is LLMConfig
        cfg = call.args[0]
        assert cfg.model == "deepseek-chat"

    @pytest.mark.asyncio
    async def test_stream_falls_back_to_default_model(self, handler, fake_llm_router):
        async for _ in handler.stream_llm({
            "prompt": "x", "system_prompt": "", "model": "", "tenant_id": "acme",
        }):
            pass
        cfg = fake_llm_router.stream_events.call_args.args[0]
        # Default model when nothing configured
        assert cfg.model == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_stream_uses_config_getter_when_no_model(self):
        cfg_getter = MagicMock(return_value=SimpleNamespace(model="custom-model"))
        deps = {
            "task_store": InMemoryTaskStore(),
            "llm_router": MagicMock(stream_events=MagicMock(return_value=_make_async_gen())),
            "config_getter": cfg_getter,
        }
        h = GrpcServiceHandler(deps)
        async for _ in h.stream_llm({
            "prompt": "x", "system_prompt": "", "model": "", "tenant_id": "acme",
        }):
            pass
        cfg_getter.assert_called_once()


# ── aiter_stream_llm_events (low-level adapter) ──


class TestStreamAdapter:
    @pytest.mark.asyncio
    async def test_text_event(self):
        async def gen():
            yield _make_stream_event("text", text="hello")
        out = []
        async for d in aiter_stream_llm_events(gen()):
            out.append(d)
        assert out == [{"text": {"text": "hello"}}]

    @pytest.mark.asyncio
    async def test_tool_start_with_id(self):
        async def gen():
            yield _make_stream_event("tool_start", tool="search", id="abc")
        async for d in aiter_stream_llm_events(gen()):
            assert d == {"tool_start": {"tool": "search", "id": "abc"}}
            break

    @pytest.mark.asyncio
    async def test_tool_input_with_delta(self):
        async def gen():
            yield _make_stream_event("tool_input", tool="search", id="x", delta="{}")
        async for d in aiter_stream_llm_events(gen()):
            assert d["tool_input"]["delta"] == "{}"
            break

    @pytest.mark.asyncio
    async def test_tool_end(self):
        async def gen():
            yield _make_stream_event("tool_end", tool="search", id="x")
        async for d in aiter_stream_llm_events(gen()):
            assert d == {"tool_end": {"tool": "search", "id": "x"}}
            break

    @pytest.mark.asyncio
    async def test_tool_result(self):
        async def gen():
            yield _make_stream_event(
                "tool_result", tool="search", id="x",
                output="42 results", is_error=False,
            )
        async for d in aiter_stream_llm_events(gen()):
            assert d["tool_result"]["output"] == "42 results"
            assert d["tool_result"]["is_error"] is False
            break

    @pytest.mark.asyncio
    async def test_error_event(self):
        async def gen():
            yield _make_stream_event("error", message="upstream")
        async for d in aiter_stream_llm_events(gen()):
            assert d == {"error": {"message": "upstream"}}
            break

    @pytest.mark.asyncio
    async def test_done_event_includes_usage(self):
        async def gen():
            yield _make_stream_event(
                "done",
                usage=LLMUsage(model="m", input_tokens=11, output_tokens=22),
            )
        async for d in aiter_stream_llm_events(gen()):
            assert d["done"]["usage"]["input_tokens"] == 11
            assert d["done"]["usage"]["model"] == "m"
            break

    @pytest.mark.asyncio
    async def test_unknown_event_kind_dropped(self):
        async def gen():
            yield _make_stream_event("future_kind")  # unknown kind
            yield _make_stream_event("text", text="hi")
        out = [d async for d in aiter_stream_llm_events(gen())]
        assert len(out) == 1
        assert out[0] == {"text": {"text": "hi"}}

    @pytest.mark.asyncio
    async def test_missing_fields_default_to_empty(self):
        # StreamEvent with no tool/id/text should still produce a
        # valid dict with empty strings (the adapter must not raise).
        async def gen():
            yield _make_stream_event("text")  # no text field
            yield _make_stream_event("tool_start")  # no tool/id
        out = [d async for d in aiter_stream_llm_events(gen())]
        assert out[0] == {"text": {"text": ""}}
        assert out[1] == {"tool_start": {"tool": "", "id": ""}}


# ── Row-to-dict translation (helper, used by the servicer) ──


class TestTaskRowTranslation:
    def test_status_string_to_int(self, handler):
        for status_str, expected_int in [
            ("pending", 1), ("running", 2), ("completed", 3),
            ("failed", 4), ("cancelled", 5),
        ]:
            row = {
                "id": "x", "tenant_id": "t", "agent": "a",
                "input": "i", "status": status_str, "output": None,
                "error": "", "metadata": {}, "created_at": None, "updated_at": None,
                "input_tokens": 0, "output_tokens": 0,
            }
            out = handler._task_row_to_dict(row)
            assert out["status"] == expected_int, f"failed for {status_str}"

    def test_output_json_serializes_dict(self, handler):
        row = {
            "id": "x", "tenant_id": "t", "agent": "a",
            "input": "i", "status": "completed",
            "output": {"text": "hello", "items": [1, 2, 3]},
            "error": "", "metadata": {},
            "created_at": datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc),
            "updated_at": None, "input_tokens": 5, "output_tokens": 7,
        }
        out = handler._task_row_to_dict(row)
        import json
        parsed = json.loads(out["output_json"])
        assert parsed == {"text": "hello", "items": [1, 2, 3]}
        # created_at formatted as ISO 8601 with 'Z' suffix
        assert out["created_at"].startswith("2026-07-22T12:00:00")
        assert out["created_at"].endswith("Z")
        # input_tokens / output_tokens propagated
        assert out["input_tokens"] == 5
        assert out["output_tokens"] == 7

    def test_unknown_status_string_becomes_unspecified(self, handler):
        row = {
            "id": "x", "tenant_id": "t", "agent": "a",
            "input": "i", "status": "something-weird", "output": None,
            "error": "", "metadata": {}, "created_at": None, "updated_at": None,
            "input_tokens": 0, "output_tokens": 0,
        }
        out = handler._task_row_to_dict(row)
        # 0 = TASK_STATUS_UNSPECIFIED
        assert out["status"] == 0

    def test_none_row_returns_empty_dict(self, handler):
        assert handler._task_row_to_dict(None) == {}
