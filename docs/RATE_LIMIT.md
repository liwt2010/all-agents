# PR-12: Per-User & Per-Scope Rate Limiting

## Status: DONE (this commit)

## Goal
Production-grade rate limiting — replace single-IP-only `RateLimitMiddleware` with:
1. **Per-user limits** (key off JWT user_id, not just IP) — prevents NAT bypass
2. **Per-scope limits** (different limits per endpoint category)
3. **Tiered composition** — request must pass user + scope + IP limits (any fail = 429)
4. **Standard `X-RateLimit-*` response headers**
5. **Sliding window log** (more accurate than token bucket for short bursts)

## What's Already There (不重写)
- `core/security_middleware.py` — `RateLimiter` (token bucket, IP-only) + `RateLimitMiddleware`
- `core/quota.py` — 4-level quota (user / department / system / llm_api) for cost/concurrency
- `core/security_middleware.py` — `SecurityHeadersMiddleware` etc.

## Gaps Fixed in This PR

| Gap | Fix |
|-----|-----|
| 仅按 IP 限流 — NAT 后 1000 人共享同一 IP | 加 per-user 桶 (从 JWT `user_id` 取 key) |
| 所有路径一个限速 — `/api/health` 不该和 `/api/tasks` 同样限 | 加 per-scope 桶 (default / expensive / heavy) |
| 没有 `X-RateLimit-Remaining` / `X-RateLimit-Reset` headers | 加 standard 限流响应头 |
| Token bucket 不适合短突发流量 | 改 sliding window log (更准的 burst 控制) |
| 限流 fail-open / fail-closed 没策略 | 加 `on_error` 配置 (默认 fail-open 防止限流本身挂掉拖死服务) |

## Limits Per Scope

| Scope | Path prefix | Per-user limit | Per-IP limit | Reason |
|-------|-------------|----------------|--------------|--------|
| `default` | `/api/agents`, `/api/graph/*` | 120/min | 240/min | Read-mostly |
| `expensive` | `/api/tasks`, `/api/discussions` | 20/min | 60/min | LLM calls |
| `heavy` | `/api/admin/*`, `/api/audit/query` | 10/min | 30/min | Cost control |
| `auth` | `/api/auth/*` | 5/min | 30/min | Anti-brute-force |

(All configurable via env vars)

## Sliding Window Log Algorithm

```
Bucket = list of timestamps (size <= limit)
On request:
    cutoff = now - window
    drop entries older than cutoff
    if len(bucket) < limit:
        append now
        return ALLOW
    else:
        oldest = bucket[0]
        retry_after = window - (now - oldest)
        return DENY (with Retry-After header)

Memory: O(limit) per key; cleanup runs every 60s for stale keys.
```

Compared to token bucket:
- More memory (O(limit) vs O(1))
- More accurate for short bursts (no token accumulation)
- Easy to compute exact `X-RateLimit-Reset` time

## Architecture

```
Request
   │
   ▼
┌──────────────────────────────────────────┐
│ RateLimitMiddleware (PR-12)             │
│   1. Resolve user_id from JWT           │
│      (falls back to IP if anonymous)    │
│   2. Determine scope from path          │
│   3. Check 3 buckets:                   │
│      - user:{user_id}:{scope}           │
│      - ip:{ip}:{scope}                  │
│      - user:{user_id}:default (global)  │
│   4. If ANY fail → 429                  │
│   5. Attach X-RateLimit-* headers       │
└─────┬────────────────────────────────────┘
      ▼
   Route handler
```

## Implementation

### Files

| File | Change | Lines |
|------|--------|-------|
| `core/rate_limit/sliding_window.py` | **新增** — `SlidingWindowLimiter` class | ~150 |
| `core/rate_limit/registry.py` | **新增** — `LimiterRegistry` + scope config | ~120 |
| `core/rate_limit/__init__.py` | **新增** — public API | ~30 |
| `core/security_middleware.py` | 加 `SlidingWindowRateLimitMiddleware` (新),保留旧 `RateLimitMiddleware` 作 deprecated shim | +120 |
| `api/server.py` | 注册新 middleware (默认开,可 `DISABLE_RATE_LIMIT=true` 关) | +10 |
| `tests/test_rate_limit_sliding.py` | **新增** | ~300 |

### Configuration

环境变量:
- `AGENT_RATE_LIMIT_ENABLED` — `true` (default) / `false`
- `AGENT_RATE_LIMIT_SCOPE_DEFAULT_USER` — `120` (req/min)
- `AGENT_RATE_LIMIT_SCOPE_DEFAULT_IP` — `240`
- `AGENT_RATE_LIMIT_SCOPE_EXPENSIVE_USER` — `20`
- `AGENT_RATE_LIMIT_SCOPE_HEAVY_USER` — `10`
- `AGENT_RATE_LIMIT_SCOPE_AUTH_USER` — `5`
- `AGENT_RATE_LIMIT_WINDOW_SECONDS` — `60` (default)
- `AGENT_RATE_LIMIT_FAIL_MODE` — `open` (default) / `closed`

### Response Headers

```
HTTP/1.1 200 OK
X-RateLimit-Limit: 120
X-RateLimit-Remaining: 87
X-RateLimit-Reset: 1688841660       (epoch seconds when bucket refills)
X-RateLimit-Scope: default

HTTP/1.1 429 Too Many Requests
X-RateLimit-Limit: 20
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1688841720
Retry-After: 42
```

## Test Plan

| Test | Coverage |
|------|----------|
| `test_sliding_window_allows_under_limit` | Basic allow |
| `test_sliding_window_denies_over_limit` | Reject 21st request in 60s window |
| `test_sliding_window_releases_after_window` | Old entries expire |
| `test_sliding_window_per_user_isolation` | User A's quota doesn't affect User B |
| `test_sliding_window_per_ip_isolation` | IP A doesn't affect IP B |
| `test_scope_based_different_limits` | /api/tasks vs /api/agents |
| `test_compose_user_ip_scope_must_all_pass` | Any of 3 fails → 429 |
| `test_anonymous_falls_back_to_ip_only` | No JWT → IP only |
| `test_response_headers_present` | X-RateLimit-* on 200 and 429 |
| `test_429_retry_after_header` | Retry-After seconds |
| `test_fail_open_when_backend_errors` | Limiter exception → allow |
| `test_cleanup_removes_stale_keys` | After window*2 idle, key removed |
| `test_scope_classification` | 4 paths → 4 scopes |
| `test_concurrent_requests_thread_safe` | asyncio.gather 100 reqs |

## Out of Scope (deferred to PR-13+)
- Distributed rate limiting (Redis-backed for multi-instance)
- Adaptive rate limits (auto-adjust based on backend health)
- Per-tenant burst allowance (free / pro / enterprise tiers)
- Quota integration (cost-based, separate from rate)