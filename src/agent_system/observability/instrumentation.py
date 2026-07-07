"""
Instrumentation decorators + helpers for observability (PR-10).

Usage:
    from agent_system.observability.instrumentation import (
        track_task, track_llm, track_storage,
    )

    @track_task(agent_type="smart")
    async def execute(self, ...):
        ...

    @track_llm(model="deepseek-chat", provider="deepseek")
    async def chat(self, messages):
        ...

    @track_storage(backend="sqlite", op="save_node")
    def save_node(self, node):
        ...

All decorators are no-ops if OBSERVABILITY_ENABLED=false (env var).
"""

import logging
import os
import time
from functools import wraps
from typing import Any, Callable, Optional

from agent_system.observability.metrics import get_metrics_registry

logger = logging.getLogger(__name__)


def _is_enabled() -> bool:
    """Check if observability is enabled (env var override)."""
    return os.environ.get("AGENT_OBSERVABILITY_ENABLED", "true").lower() in ("1", "true", "yes")


# ── Standard metric names (exported as constants for query convenience) ──

HTTP_REQUESTS_TOTAL = "agent_http_requests_total"
HTTP_REQUEST_DURATION = "agent_http_request_duration_seconds"
TASKS_TOTAL = "agent_tasks_total"
TASK_DURATION = "agent_task_duration_seconds"
LLM_REQUESTS_TOTAL = "agent_llm_requests_total"
LLM_TOKENS_TOTAL = "agent_llm_tokens_total"
LLM_REQUEST_DURATION = "agent_llm_request_duration_seconds"
STORAGE_OPS_TOTAL = "agent_storage_ops_total"
STORAGE_OP_DURATION = "agent_storage_op_duration_seconds"
ACTIVE_TASKS = "agent_active_tasks"
MEMORY_NODES_TOTAL = "agent_memory_nodes_total"


# ── Task decorator ──

def track_task(agent_type: str = "default") -> Callable:
    """
    Decorator that records task execution metrics.

    Increments:
        agent_tasks_total{agent_type, status}
    Records:
        agent_task_duration_seconds{agent_type}
    Updates:
        agent_active_tasks (gauge, +/-1)
    """
    def decorator(func: Callable) -> Callable:
        if not _is_enabled():
            return func

        registry = get_metrics_registry()
        tasks_total = registry.counter(
            TASKS_TOTAL,
            "Total agent tasks executed",
            ["agent_type", "status"],
        )
        task_duration = registry.histogram(
            TASK_DURATION,
            "Agent task execution duration in seconds",
            label_names=["agent_type"],
        )
        active_tasks = registry.gauge(
            ACTIVE_TASKS,
            "Currently active agent tasks",
            ["agent_type"],
        )

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            active_tasks.inc(1.0, agent_type=agent_type)
            started = time.perf_counter()
            status = "success"
            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as e:
                status = "failure"
                tasks_total.inc(1.0, agent_type=agent_type, status=status)
                task_duration.observe(time.perf_counter() - started, agent_type=agent_type)
                active_tasks.dec(1.0, agent_type=agent_type)
                raise
            finally:
                if status == "success":
                    tasks_total.inc(1.0, agent_type=agent_type, status=status)
                task_duration.observe(time.perf_counter() - started, agent_type=agent_type)
                active_tasks.dec(1.0, agent_type=agent_type)

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            active_tasks.inc(1.0, agent_type=agent_type)
            started = time.perf_counter()
            status = "success"
            try:
                result = func(*args, **kwargs)
                return result
            except Exception:
                status = "failure"
                tasks_total.inc(1.0, agent_type=agent_type, status=status)
                task_duration.observe(time.perf_counter() - started, agent_type=agent_type)
                active_tasks.dec(1.0, agent_type=agent_type)
                raise
            finally:
                if status == "success":
                    tasks_total.inc(1.0, agent_type=agent_type, status=status)
                task_duration.observe(time.perf_counter() - started, agent_type=agent_type)
                active_tasks.dec(1.0, agent_type=agent_type)

        import inspect
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


# ── LLM decorator ──

def track_llm(model: str = "unknown", provider: str = "unknown") -> Callable:
    """
    Decorator that records LLM call metrics.

    Increments:
        agent_llm_requests_total{model, provider, status}
        agent_llm_tokens_total{model, type}  (type=input|output, from result.usage)
    Records:
        agent_llm_request_duration_seconds{model}
    """
    def decorator(func: Callable) -> Callable:
        if not _is_enabled():
            return func

        registry = get_metrics_registry()
        llm_total = registry.counter(
            LLM_REQUESTS_TOTAL,
            "Total LLM API requests",
            ["model", "provider", "status"],
        )
        llm_tokens = registry.counter(
            LLM_TOKENS_TOTAL,
            "Total LLM tokens consumed",
            ["model", "type"],
        )
        llm_duration = registry.histogram(
            LLM_REQUEST_DURATION,
            "LLM API request duration in seconds",
            label_names=["model"],
        )

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            started = time.perf_counter()
            status = "success"
            try:
                result = await func(*args, **kwargs)
                # Try to extract usage info from common result shapes
                usage = _extract_usage(result)
                if usage:
                    if usage.get("input_tokens"):
                        llm_tokens.inc(usage["input_tokens"], model=model, type="input")
                    if usage.get("output_tokens"):
                        llm_tokens.inc(usage["output_tokens"], model=model, type="output")
                return result
            except Exception:
                status = "failure"
                raise
            finally:
                llm_total.inc(1.0, model=model, provider=provider, status=status)
                llm_duration.observe(time.perf_counter() - started, model=model)

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            started = time.perf_counter()
            status = "success"
            try:
                result = func(*args, **kwargs)
                usage = _extract_usage(result)
                if usage:
                    if usage.get("input_tokens"):
                        llm_tokens.inc(usage["input_tokens"], model=model, type="input")
                    if usage.get("output_tokens"):
                        llm_tokens.inc(usage["output_tokens"], model=model, type="output")
                return result
            except Exception:
                status = "failure"
                raise
            finally:
                llm_total.inc(1.0, model=model, provider=provider, status=status)
                llm_duration.observe(time.perf_counter() - started, model=model)

        import inspect
        if inspect.iscoroutinefunction(func):
            return async_wrapper
        return sync_wrapper

    return decorator


def _extract_usage(result: Any) -> Optional[dict]:
    """Extract token usage from common LLM result shapes (OpenAI / Anthropic / DeepSeek)."""
    if result is None:
        return None
    # OpenAI / DeepSeek shape: result.usage.prompt_tokens, completion_tokens
    usage = getattr(result, "usage", None)
    if usage is not None:
        return {
            "input_tokens": getattr(usage, "prompt_tokens", 0) or getattr(usage, "input_tokens", 0),
            "output_tokens": getattr(usage, "completion_tokens", 0) or getattr(usage, "output_tokens", 0),
        }
    # Dict shape
    if isinstance(result, dict):
        u = result.get("usage")
        if u:
            return {
                "input_tokens": u.get("prompt_tokens", 0) or u.get("input_tokens", 0),
                "output_tokens": u.get("completion_tokens", 0) or u.get("output_tokens", 0),
            }
    return None


# ── Storage decorator ──

def track_storage(backend: str = "unknown", op: str = "unknown") -> Callable:
    """
    Decorator that records storage backend operation metrics.

    Increments:
        agent_storage_ops_total{backend, op, result}
    Records:
        agent_storage_op_duration_seconds{backend, op}
    """
    def decorator(func: Callable) -> Callable:
        if not _is_enabled():
            return func

        registry = get_metrics_registry()
        ops_total = registry.counter(
            STORAGE_OPS_TOTAL,
            "Total storage backend operations",
            ["backend", "op", "result"],
        )
        op_duration = registry.histogram(
            STORAGE_OP_DURATION,
            "Storage backend operation duration in seconds",
            label_names=["backend", "op"],
        )

        @wraps(func)
        def wrapper(*args, **kwargs):
            started = time.perf_counter()
            result_label = "ok"
            try:
                result = func(*args, **kwargs)
                return result
            except Exception:
                result_label = "fail"
                raise
            finally:
                ops_total.inc(1.0, backend=backend, op=op, result=result_label)
                op_duration.observe(time.perf_counter() - started, backend=backend, op=op)

        return wrapper

    return decorator


# ── Helper for memory node count gauge ──

def update_memory_node_gauge(counts_by_type: dict) -> None:
    """Update agent_memory_nodes_total gauge from a {type: count} dict."""
    if not _is_enabled():
        return
    registry = get_metrics_registry()
    gauge = registry.gauge(
        MEMORY_NODES_TOTAL,
        "Current memory node count by type",
        ["type"],
    )
    for node_type, count in counts_by_type.items():
        gauge.set(float(count), type=node_type)