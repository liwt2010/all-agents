"""
gRPC service implementation (PR v0.5.0).

This module is the gRPC transport adapter for the Agent System. The
core RPCs (`SubmitTask`, `GetTask`, `ListTasks`, `StreamLLM`) all
delegate to the same in-process state and routers the REST API uses
— TaskStore for tasks, LLMRouter.stream_events() for streaming.

The actual gRPC server is started in `agent_system.grpc.server` and
is ONLY loaded when `grpcio` is importable. The function
`is_grpc_available()` lets callers (CLI, main) detect this without
importing the heavy grpcio module.

When grpcio is not installed, the imports succeed but `serve()`
raises a clear error. The handlers themselves (`GrpcServiceHandler`)
are always available so tests can call them directly without
needing the gRPC transport.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def is_grpc_available() -> bool:
    """True iff grpcio + grpcio-tools are installed.

    Cheap probe: a real `import grpc` is the only check. We deliberately
    do NOT import grpc.protos at this layer so the fast path is
    under 1 ms.
    """
    try:
        import grpc  # noqa: F401
    except ImportError:
        return False
    return True


# ── LLMEvent adapter ──

# Wire the existing StreamEvent (defined in core/llm_router.py) to a
# transport-neutral dict. The gRPC servicer transforms this dict to
# protobuf messages; future transports (JSON-RPC, gRPC-Web) can
# reuse the same dict.
async def aiter_stream_llm_events(
    stream: AsyncIterator[Any],
) -> AsyncIterator[dict[str, Any]]:
    """Adapt StreamEvent -> dict for the gRPC servicer.

    Async generator. Callers iterate with `async for`. The function is
    `async def` so the body can use `async for / yield`; the
    returned object IS the async generator (no extra `await` needed).

    The dict shape matches the `oneof body` in `agent_system.proto`:

      {"text": {"text": "..."}}
      {"tool_start": {"tool": "...", "id": "..."}}
      {"tool_input": {"tool": "...", "id": "...", "delta": "..."}}
      {"tool_end": {"tool": "...", "id": "..."}}
      {"tool_result": {"tool": "...", "id": "...", "output": "...", "is_error": ...}}
      {"error": {"message": "..."}}
      {"done": {"usage": {"input_tokens": ..., "output_tokens": ...,
                          "model": "...", "mock": ...}}}
    """
    async for ev in stream:
        if ev.kind == "text":
            yield {"text": {"text": ev.text or ""}}
        elif ev.kind == "tool_start":
            yield {"tool_start": {"tool": ev.tool or "", "id": ev.id or ""}}
        elif ev.kind == "tool_input":
            yield {
                "tool_input": {
                    "tool": ev.tool or "",
                    "id": ev.id or "",
                    "delta": ev.delta or "",
                }
            }
        elif ev.kind == "tool_end":
            yield {"tool_end": {"tool": ev.tool or "", "id": ev.id or ""}}
        elif ev.kind == "tool_result":
            yield {
                "tool_result": {
                    "tool": ev.tool or "",
                    "id": ev.id or "",
                    "output": ev.output or "",
                    "is_error": ev.is_error,
                }
            }
        elif ev.kind == "error":
            yield {"error": {"message": ev.message or "unknown error"}}
        elif ev.kind == "done":
            u = ev.usage
            yield {
                "done": {
                    "usage": {
                        "input_tokens": getattr(u, "input_tokens", 0) or 0,
                        "output_tokens": getattr(u, "output_tokens", 0) or 0,
                        "cache_read_tokens": getattr(u, "cache_read_tokens", 0) or 0,
                        "cache_creation_tokens": getattr(u, "cache_creation_tokens", 0) or 0,
                        "duration_ms": getattr(u, "duration_ms", 0.0) or 0.0,
                        "model": getattr(u, "model", "") or "",
                        "mock": getattr(u, "mock", False),
                    }
                }
            }
        # Unknown event kinds are dropped (forward compat).


# ── Handlers (transport-agnostic) ──

class GrpcServiceHandler:
    """Pure-async RPC implementations.

    No gRPC types leak in here: handlers take dicts in, yield dicts
    out. The gRPC servicer (built from generated `_pb2_grpc`) just
    adapts the dicts to protobuf and back. This means:

      - Tests can call these handlers directly without any gRPC dep.
      - Future transports (JSON-RPC, HTTP/3, in-process bus) can
        reuse the same handlers.
      - The gRPC layer becomes a thin shim, not the source of truth.

    Handlers take a `deps` dict at construction time so the same
    handler can be used in production (with the real TaskStore +
    LLMRouter) and in tests (with mocks).
    """

    def __init__(self, deps: dict[str, Any]):
        self.deps = deps
        # Required dependencies (injected at startup):
        self._task_store = deps["task_store"]
        self._llm_router = deps["llm_router"]
        self._config_getter = deps.get("config_getter")  # optional callable -> LLMConfig
        self._require_auth = deps.get("require_auth")    # optional (token) -> user/None

    # ── SubmitTask ──

    async def submit_task(self, request: dict[str, Any]) -> dict[str, Any]:
        """Submit a task. Returns the Task dict with status=PENDING."""
        tenant_id = request.get("tenant_id", "default")
        agent = request.get("agent", "product")
        inp = request.get("input", "")
        metadata = request.get("metadata", {})

        # Persist a Task row via the same TaskStore the REST API uses.
        task = self._task_store.create(
            tenant_id=tenant_id,
            agent=agent,
            input_text=inp,
            metadata=dict(metadata or {}),
        )

        # Fire-and-forget background execution. Errors surface via
        # the task row (status=FAILED, error=...) so the caller
        # can poll GetTask.
        asyncio.create_task(
            self._run_task(task["id"], tenant_id, agent, inp, metadata)
        )
        return self._task_row_to_dict(task)

    async def _run_task(self, task_id, tenant_id, agent, input_text, metadata):
        """Background task execution. Mirrors the REST tasks.py handler."""
        from agent_system.core.llm_router import LLMConfig  # local import to avoid cycle
        from agent_system.core.agent import TaskContext

        try:
            cfg = self._config_getter() if self._config_getter else LLMConfig(model="claude-haiku-4-5-20251001")
            messages = [{"role": "user", "content": input_text}]
            system = metadata.get("system_prompt", "You are helpful.")
            text, _usage = await self._llm_router.call_llm(
                cfg, system, messages, tools=None,
            )
            self._task_store.complete(
                task_id=task_id, tenant_id=tenant_id, output_text=text,
            )
        except Exception as e:
            logger.warning(f"gRPC task {task_id} failed: {e}")
            self._task_store.fail(task_id=task_id, tenant_id=tenant_id, error=str(e))

    # ── GetTask ──

    async def get_task(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Return the task or None if not found / wrong tenant."""
        tenant_id = request.get("tenant_id", "default")
        task_id = request.get("id", "")
        row = self._task_store.get(task_id, tenant_id=tenant_id)
        return self._task_row_to_dict(row) if row else None

    # ── ListTasks ──

    async def list_tasks(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Stream tasks in cursor order. Yields ListTasksResponse dicts.

        The servicer builds one ListTasksResponse per page; this generator
        yields those dicts. The client sees a continuous stream of tasks
        and only the LAST response carries a `next_cursor` (used for
        server-side pagination).
        """
        tenant_id = request.get("tenant_id", "default")
        status = request.get("status", 0)  # 0 = all
        limit = int(request.get("limit", 50))
        cursor = request.get("cursor", "") or None

        page = self._task_store.list(
            tenant_id=tenant_id, status=status or None, limit=limit, cursor=cursor,
        )
        # Yield a single response per page. The gRPC client sees
        # these as a stream and can stop early (e.g. UI showing only
        # the first N). For our TaskStore (SQLite) the whole result
        # set usually fits in one page; for PostgreSQL this naturally
        # paginates.
        yield {
            "tasks": [self._task_row_to_dict(r) for r in page["rows"]],
            "next_cursor": page.get("next_cursor", "") or "",
        }

    # ── StreamLLM ──

    async def stream_llm(self, request: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Stream LLM events. The dict shape matches the proto oneof.

        Adapts the v0.4.0 StreamEvent from core/llm_router to the
        transport-neutral dict format the gRPC servicer consumes.
        """
        from agent_system.core.llm_router import LLMConfig

        prompt = request.get("prompt", "")
        system = request.get("system_prompt") or "You are helpful."
        model = request.get("model")
        if model:
            cfg = LLMConfig(model=model)
        elif self._config_getter:
            cfg = self._config_getter()
        else:
            cfg = LLMConfig(model="claude-haiku-4-5-20251001")
        messages = [{"role": "user", "content": prompt}]

        stream = self._llm_router.stream_events(cfg, system, messages)
        async for ev_dict in aiter_stream_llm_events(stream):
            yield ev_dict

    # ── helpers ──

    @staticmethod
    def _task_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
        """Translate a TaskStore row into the wire dict the servicer emits.

        Mirrors `agent_system.api.routes.tasks._task_to_response` —
        kept independent so the gRPC and REST shapes can diverge
        independently if needed (e.g. gRPC can add internal fields
        that REST hides).
        """
        if row is None:
            return {}
        status_int = {
            "pending": 1, "running": 2, "completed": 3,
            "failed": 4, "cancelled": 5,
        }.get(str(row.get("status", "")).lower(), 0)
        output_json = ""
        if row.get("output"):
            try:
                output_json = json.dumps(row["output"], default=str)
            except TypeError:
                output_json = str(row["output"])
        # created_at / updated_at as ISO 8601 strings — the gRPC
        # servicer will map these to google.protobuf.Timestamp.
        return {
            "id": row.get("id", ""),
            "status": status_int,
            "input": row.get("input", "") or "",
            "agent": row.get("agent", "") or "",
            "output_json": output_json,
            "error": row.get("error", "") or "",
            "created_at": _iso(row.get("created_at")),
            "updated_at": _iso(row.get("updated_at")),
            "input_tokens": int(row.get("input_tokens", 0) or 0),
            "output_tokens": int(row.get("output_tokens", 0) or 0),
        }


def _iso(value: Any) -> str:
    """Render a datetime as RFC 3339 (with 'Z' suffix)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat().replace("+00:00", "Z")
    return str(value)
