# PR-10: Prometheus Metrics 加固

## Status: DONE (cb7d8f8 + this commit)

## Goal
生产化 Prometheus 指标层 — 既有 `observability/metrics.py` 已经有 Counter/Gauge/Histogram + `MetricsRegistry`,
本 PR 把指标接到 HTTP / Agent 执行链路上,并暴露标准的 `/metrics` 端点供 Prometheus 抓取。

## What's Already There (不重写)
- `observability/metrics.py` — `Counter` / `Gauge` / `Histogram` / `MetricsRegistry`
- `observability/tracing.py` — `Span` / `Tracer`
- `api/server.py:467-489` — `/api/metrics` JSON + `/api/metrics/prometheus` text

## Gaps Fixed in This PR
| Gap | Fix |
|-----|-----|
| 没有标准 `/metrics` 端点 (Prometheus 默认) | 加 `GET /metrics` 返回纯 text/plain (Prometheus exposition format) |
| 没有 HTTP request latency 指标 | 加 `MetricsMiddleware` 记录每个请求的 latency / status / method / path |
| 没有 task 执行计数 / 时长 | 加 `agent_tasks_total{status}` counter + `agent_task_duration_seconds` histogram |
| 没有 LLM token / cost 指标 | 加 `agent_llm_tokens_total{model,type}` counter (input/output) |
| 没有 storage 操作指标 | 加 `agent_storage_ops_total{backend,op}` counter + `agent_storage_op_duration_seconds` histogram |
| `api/server.py` 没有 instrument middleware | 接 RequestIDMiddleware 模式,加 `MetricsMiddleware` |

## Metrics Inventory

### HTTP layer
- `agent_http_requests_total{method,path,status}` — Counter
- `agent_http_request_duration_seconds{method,path}` — Histogram

### Agent layer
- `agent_tasks_total{agent_type,status}` — Counter (success/failure/escalated)
- `agent_task_duration_seconds{agent_type}` — Histogram

### LLM layer
- `agent_llm_requests_total{model,provider}` — Counter
- `agent_llm_tokens_total{model,type}` — Counter (type=input|output)
- `agent_llm_request_duration_seconds{model}` — Histogram

### Storage layer (per PR-9 backend)
- `agent_storage_ops_total{backend,op,result}` — Counter (op=save|load|delete|list, result=ok|fail)
- `agent_storage_op_duration_seconds{backend,op}` — Histogram

### System
- `agent_active_tasks` — Gauge (current in-flight)
- `agent_memory_nodes_total{type}` — Gauge (current node count by type)

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ HTTP Request                                                │
└─────┬───────────────────────────────────────────────────────┘
      ▼
┌─────────────────────────────┐
│ RequestIDMiddleware (PR-7)  │ — sets X-Request-ID + contextvar
└─────┬───────────────────────┘
      ▼
┌─────────────────────────────┐
│ MetricsMiddleware (PR-10)   │ — wraps every request, records:
│                             │   - method, path, status, duration
│                             │   - injects request_id into metrics labels
└─────┬───────────────────────┘
      ▼
┌─────────────────────────────┐
│ Other middleware...         │
└─────┬───────────────────────┘
      ▼
┌─────────────────────────────┐
│ Route handler               │ — uses @track_task / @track_llm decorators
└─────────────────────────────┘

         ▼
┌─────────────────────────────┐
│ MetricsRegistry             │ — process-local in-memory store
│   Counter / Gauge /         │
│   Histogram                 │
└─────┬───────────────────────┘
      ▼
┌─────────────────────────────┐
│ /metrics (text/plain)       │ — Prometheus exposition format
│ /api/metrics (JSON)         │ — human-readable, dashboard use
└─────────────────────────────┘
```

## Implementation

### Files

| File | Change | Lines |
|------|--------|-------|
| `src/agent_system/observability/metrics.py` | 已存在,补 `summary()` 方法 + `reset_all()` | +20 |
| `src/agent_system/observability/instrumentation.py` | **新增** — decorators + helper functions for agent/llm/storage instrumentation | ~180 |
| `src/agent_system/core/metrics_middleware.py` | **新增** — FastAPI middleware | ~80 |
| `src/agent_system/core/llm_router.py` | 补 instrumentation hooks in `get_api_client()` + call sites | +30 |
| `src/agent_system/agents/custom/base.py` | 补 `track_task` decorator on `execute()` | +15 |
| `src/agent_system/memory/storage/*.py` | 补 instrumentation hook 调用 | +50 (5 files) |
| `src/agent_system/api/server.py` | 注册 MetricsMiddleware + `GET /metrics` 端点 | +40 |
| `tests/test_metrics_instrumentation.py` | **新增** — 单元 + 集成测试 | ~250 |

### Instrumentation Pattern

```python
# Agent
@track_task(agent_type="smart")
async def execute(self, ...):
    ...

# LLM
@track_llm(model="deepseek-chat")
async def chat(self, messages):
    ...

# Storage
@track_storage(backend="sqlite", op="save_node")
def save_node(self, node):
    ...
```

Decorators are no-ops if `OBSERVABILITY_ENABLED=false` (env var).

## Test Plan

| Test | Coverage |
|------|----------|
| `test_metrics_counter_inc` | Basic Counter |
| `test_metrics_gauge_set` | Basic Gauge |
| `test_metrics_histogram_observe` | Buckets + sum + count |
| `test_metrics_middleware_records_request` | 200/4xx/5xx all increment counter |
| `test_metrics_middleware_uses_request_id` | X-Request-ID propagates to labels |
| `test_track_task_decorator` | success/failure increment correct counter |
| `test_track_llm_decorator` | tokens counter + duration histogram |
| `test_track_storage_decorator` | backend + op labels |
| `test_prometheus_endpoint_format` | Valid Prometheus text exposition |
| `test_observability_disabled_is_noop` | env var off → no metric changes |

## Out of Scope (deferred to PR-12+)
- Distributed tracing (OpenTelemetry OTLP export)
- Push gateway (only pull model)
- Per-tenant metrics (requires label cardinality management)
- Histogram exemplar support (links to traces)