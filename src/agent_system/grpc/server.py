"""
gRPC server entry point (PR v0.5.0).

`python -m agent_system.grpc.server` starts a gRPC listener on
:50051 (override with `AGENT_GRPC_PORT`). The server proxies the
four RPCs in `agent_system.proto` to the same in-process
`TaskStore` + `LLMRouter` the REST/WS APIs use.

When `grpcio` is not installed, this script prints a clear error
and exits 2 — the REST API is unaffected.

When `grpcio-tools` hasn't been run yet, this script also prints
a hint to run the protoc compiler. We do NOT pre-generate the
_pb2 modules and check them in (they're 200+ KB of auto-generated
code that would bloat the repo and create merge-noise on every
proto change). Run the compiler once on your dev machine, then
`make grpc` (TBD) or `python -m agent_system.grpc.codegen`.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from concurrent import futures
from typing import Any

from agent_system.grpc.handlers import GrpcServiceHandler, is_grpc_available

logger = logging.getLogger(__name__)


def _build_patches() -> tuple:
    """Return a (Servicer, _pb2, _pb2_grpc) tuple for the gRPC servicer.

    The returned Servicer class is built dynamically to avoid
    hard-importing the generated _pb2_grpc module (which only
    exists after running protoc).
    """
    try:
        from agent_system.grpc import agent_system_pb2_grpc  # type: ignore
        from agent_system.grpc import agent_system_pb2  # type: ignore
    except ImportError:
        raise RuntimeError(
            "agent_system.grpc._pb2_grpc is not generated. "
            "Run:\n"
            "  python -m grpc_tools.protoc -I src/agent_system/grpc/proto "
            "  --python_out=src/agent_system/grpc "
            "  --grpc_python_out=src/agent_system/grpc "
            "  src/agent_system/grpc/proto/agent_system.proto"
        )

    class AgentSystemServicer(agent_system_pb2_grpc.AgentSystemServiceServicer):
        def __init__(self, handler: GrpcServiceHandler):
            self._h = handler

        def SubmitTask(self, request, context):
            import asyncio
            req = {
                "input": request.input,
                "agent": request.agent,
                "tenant_id": request.tenant_id,
                "metadata": _struct_to_dict(request.metadata),
            }
            row = asyncio.run(self._h.submit_task(req))
            return _dict_to_task_message(row, agent_system_pb2)

        def GetTask(self, request, context):
            import asyncio
            import grpc
            req = {"id": request.id, "tenant_id": request.tenant_id}
            row = asyncio.run(self._h.get_task(req))
            if row is None:
                context.abort(grpc.StatusCode.NOT_FOUND, "task not found")
                return agent_system_pb2.Task()
            return _dict_to_task_message(row, agent_system_pb2)

        def ListTasks(self, request, context):
            import asyncio
            req = {
                "tenant_id": request.tenant_id,
                "status": request.status,
                "limit": request.limit,
                "cursor": request.cursor,
            }
            # The generated servicer passes this generator back to
            # grpc; gRPC pulls the next value when it needs to send
            # the next stream message. We bridge by wrapping the
            # async generator in a sync one via asyncio.run per
            # value, which is the canonical pattern for sync-from-async
            # gRPC servicer methods.
            for page in self._iter_list_tasks(req, context):
                yield page

        def _iter_list_tasks(self, req, context):
            import asyncio
            async def _collect():
                out = []
                async for page in self._h.list_tasks(req):
                    out.append(page)
                return out
            pages = asyncio.run(_collect())
            for page in pages:
                yield agent_system_pb2.ListTasksResponse(
                    tasks=[_dict_to_task_message(t, agent_system_pb2) for t in page["tasks"]],
                    next_cursor=page.get("next_cursor", "") or "",
                )

        def StreamLLM(self, request, context):
            return self._iter_stream_llm(request, context)

        def _iter_stream_llm(self, request, context):
            import asyncio
            req = {
                "prompt": request.prompt,
                "system_prompt": request.system_prompt,
                "model": request.model,
                "tenant_id": request.tenant_id,
            }
            async def _collect():
                out = []
                async for ev in self._h.stream_llm(req):
                    out.append(ev)
                return out
            events = asyncio.run(_collect())
            for ev in events:
                yield _dict_to_llm_event(ev, agent_system_pb2)

    return AgentSystemServicer, agent_system_pb2, agent_system_pb2_grpc


def _struct_to_dict(struct) -> dict[str, Any]:
    """google.protobuf.Struct -> dict (lossy: drops unknown fields)."""
    if struct is None:
        return {}
    out: dict[str, Any] = {}
    for k, v in struct.fields.items():
        if v.kind == v.VK_STRUCT:
            out[k] = _struct_to_dict(v.struct_value)
        elif v.kind == v.VK_LIST:
            out[k] = [_struct_to_dict(x.struct_value) for x in v.list_value]
        else:
            out[k] = v.string_value or v.number_value or v.bool_value
    return out


def _dict_to_task_message(row: dict[str, Any], _pb2) -> Any:
    """Translate a Task dict into the proto Task message."""
    msg = _pb2.Task(
        id=row.get("id", ""),
        status=row.get("status", 0),
        input=row.get("input", ""),
        agent=row.get("agent", ""),
        output_json=row.get("output_json", ""),
        error=row.get("error", ""),
        input_tokens=row.get("input_tokens", 0),
        output_tokens=row.get("output_tokens", 0),
    )
    return msg


def _dict_to_llm_event(ev: dict[str, Any], _pb2) -> Any:
    """Translate a dict-shaped LLM event into a proto LLMEvent.

    The dict always has exactly one key (the oneof discriminator).
    Unknown kinds become an error event so the client sees *something*
    rather than silently dropping the event.
    """
    msg = _pb2.LLMEvent()
    if "text" in ev:
        msg.text.text = ev["text"].get("text", "")
    elif "tool_start" in ev:
        msg.tool_start.tool = ev["tool_start"].get("tool", "")
        msg.tool_start.id = ev["tool_start"].get("id", "")
    elif "tool_input" in ev:
        msg.tool_input.tool = ev["tool_input"].get("tool", "")
        msg.tool_input.id = ev["tool_input"].get("id", "")
        msg.tool_input.delta = ev["tool_input"].get("delta", "")
    elif "tool_end" in ev:
        msg.tool_end.tool = ev["tool_end"].get("tool", "")
        msg.tool_end.id = ev["tool_end"].get("id", "")
    elif "tool_result" in ev:
        msg.tool_result.tool = ev["tool_result"].get("tool", "")
        msg.tool_result.id = ev["tool_result"].get("id", "")
        msg.tool_result.output = ev["tool_result"].get("output", "")
        msg.tool_result.is_error = ev["tool_result"].get("is_error", False)
    elif "error" in ev:
        msg.error.message = ev["error"].get("message", "")
    elif "done" in ev:
        u = ev["done"].get("usage", {})
        msg.done.usage.input_tokens = u.get("input_tokens", 0)
        msg.done.usage.output_tokens = u.get("output_tokens", 0)
        msg.done.usage.cache_read_tokens = u.get("cache_read_tokens", 0)
        msg.done.usage.cache_creation_tokens = u.get("cache_creation_tokens", 0)
        msg.done.usage.duration_ms = u.get("duration_ms", 0)
        msg.done.usage.model = u.get("model", "")
        msg.done.usage.mock = u.get("mock", False)
    else:
        # Unknown kind — emit a generic error so consumers notice.
        msg.error.message = f"unknown event shape: {list(ev.keys())}"
    return msg


def serve(host: str = "0.0.0.0", port: int = 50051,
          max_workers: int = 16) -> None:
    """Start a gRPC server and block until SIGINT.

    Raises `RuntimeError` if grpcio is not installed or the proto
    hasn't been compiled.
    """
    if not is_grpc_available():
        raise RuntimeError(
            "grpcio is not installed. Install it with:\n"
            "  pip install grpcio grpcio-tools\n"
            "and run the protoc compiler (see module docstring)."
        )

    import grpc  # noqa: F401  (presence checked above)
    from agent_system.api.state import (
        get_task_store_singleton,
        get_auth_service_singleton,
    )
    from agent_system.config.settings import get_settings
    from agent_system.core.llm_router import router as llm_router

    AgentSystemServicer, _pb2, _pb2_grpc = _build_patches()
    settings = get_settings()
    deps = {
        "task_store": get_task_store_singleton(),
        "llm_router": llm_router,
        "config_getter": lambda: settings.llm,
        "require_auth": get_auth_service_singleton().verify_token,
    }
    handler = GrpcServiceHandler(deps)
    servicer = AgentSystemServicer(handler)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    _pb2_grpc.add_AgentSystemServiceServicer_to_server(servicer, server)
    bind = f"{host}:{port}"
    server.add_insecure_port(bind)
    server.start()
    logger.info("gRPC server listening on %s", bind)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows / restricted env — signal handler not available
            pass

    async def _wait():
        await stop.wait()
        server.stop(grace=5)

    try:
        loop.run_until_complete(_wait())
    finally:
        server.stop(grace=5)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Agent System gRPC server.")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("AGENT_GRPC_PORT", "50051")),
                        help="Bind port (default: 50051, env: AGENT_GRPC_PORT)")
    parser.add_argument("--max-workers", type=int, default=16,
                        help="Thread pool size for gRPC (default: 16)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        serve(args.host, args.port, args.max_workers)
    except RuntimeError as e:
        logger.error(f"failed to start gRPC server: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
