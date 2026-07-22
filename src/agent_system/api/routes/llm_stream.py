"""Streaming LLM WebSocket endpoint (PR v0.2.0).

`GET /api/ws/llm/stream?token=...&prompt=...` upgrades to a WebSocket
and emits text chunks as the LLM produces them. Used for chat UX where
the user sees tokens arrive incrementally instead of waiting for the
full response.

Wire format (server -> client):
  {"type": "chunk", "data": "<text>"}     # one or more
  {"type": "done",  "data": {"input_tokens": .., "output_tokens": ..,
                              "duration_ms": .., "model": "..."}}
  {"type": "error", "data": "<message>"}
  {"type": "ping"}                          # keep-alive every 15s

Auth: Bearer token via `?token=` query param (browsers can't set
headers on WS upgrade). Same JWT verification as the HTTP API.
"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from agent_system.api.state import get_auth_service_singleton
from agent_system.config.settings import get_settings
from agent_system.core.llm_router import router as llm_router

logger = logging.getLogger(__name__)

router = APIRouter(tags=["llm"])

_PING_INTERVAL_SECONDS = 15.0


@router.websocket("/api/ws/llm/stream")
async def llm_stream_ws(ws: WebSocket) -> None:
    """Stream LLM tokens over a WebSocket.

    Query params:
      - token:   Bearer JWT (required)
      - prompt:  user message (required)
      - system:  optional system prompt (default: 'You are helpful.')
    """
    auth_service = get_auth_service_singleton()
    token = ws.query_params.get("token")
    prompt = ws.query_params.get("prompt", "").strip()
    system_prompt = ws.query_params.get("system", "You are helpful.").strip() or "You are helpful."

    if not token:
        await ws.close(code=1008)
        return
    payload = auth_service.verify_token(token)
    if not payload:
        await ws.close(code=1008)
        return
    if not prompt:
        await ws.accept()
        await ws.send_text(json.dumps({"type": "error", "data": "missing prompt"}))
        await ws.close(code=1008)
        return

    await ws.accept()

    settings = get_settings()
    config = settings.llm
    messages = [{"role": "user", "content": prompt}]

    # Cancellation: the WebSocket disconnect raises WebSocketDisconnect
    # at the next iteration of any pending await — so the generator
    # below will be cancelled when the client goes away.
    async def keepalive():
        try:
            while True:
                await asyncio.sleep(_PING_INTERVAL_SECONDS)
                await ws.send_text(json.dumps({"type": "ping"}))
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass

    keepalive_task = asyncio.create_task(keepalive())

    try:
        async for item in llm_router.stream_chunks(
            config, system_prompt, messages,
            _agent_name="llm_stream",
            _task_id=payload.sub,
        ):
            # StreamEnd sentinel: emit the final usage message
            if not isinstance(item, str):
                usage = item.usage
                await ws.send_text(json.dumps({
                    "type": "done",
                    "data": {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "duration_ms": usage.duration_ms,
                        "model": usage.model,
                        "mock": usage.mock,
                    },
                }))
                break
            await ws.send_text(json.dumps({"type": "chunk", "data": item}))
    except WebSocketDisconnect:
        logger.debug("LLM stream WS: client disconnected")
    except Exception as e:
        logger.warning(f"LLM stream WS error: {e}")
        try:
            await ws.send_text(json.dumps({"type": "error", "data": str(e)}))
        except Exception:
            pass
    finally:
        keepalive_task.cancel()
        try:
            await keepalive_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await ws.close()
        except Exception:
            pass