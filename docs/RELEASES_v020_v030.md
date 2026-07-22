# v0.2.0 / v0.3.0 release notes

These are the **exact release notes** for both versions, ready
to paste into the GitHub release description field on
https://github.com/liwt2010/all-agents/releases.

## v0.2.0 — 2026-07-22 (production-hardening milestone)

**Git tag:** `v0.2.0` (commit `2f396d97`)
**Scope:** backward-compatible additions; no breaking changes.

This release closes the v0.2.0 roadmap: five PRs that move the
platform from single-replica dev to multi-replica production.

| PR | Title | Impact |
|---|---|---|
| RS256 | RS256 JWT + JWKS endpoint | Multi-issuer/multi-tenant auth, external verifiers |
| Redis | Pluggable rate-limit backend + Redis | Multi-replica safe limits |
| OTel | FastAPI auto-instrumentation | Per-route spans in collector |
| RLS | PostgreSQL Row-Level Security | Schema-level tenant isolation |
| WS | WebSocket streaming LLM | Token-by-token chat UX |

### RS256 JWT + JWKS endpoint

`AuthService` auto-detects algorithm from env:
- `AUTH_PRIVATE_KEY` set → RS256 (asymmetric, recommended for
  multi-replica / multi-tenant / external verifiers)
- Otherwise → HS256 (legacy, backward-compatible)

Public keys are distributed via `GET /api/auth/jwks` (RFC 7517)
so external services can verify tokens locally without contacting
the auth server. `scripts/gen_rsa_keys.py` generates 2048/3072/4096-bit
keypairs with `--env-file` mode that writes the config directly.

### Redis-backed rate limit

Pluggable `RateLimiterBackend`:
- `InMemoryBackend` — default, single-process
- `RedisBackend` — multi-replica safe. ZSET + Lua for atomic
  check-and-record. `REDIS_URL` env switches it on; falls back to
  in-memory if Redis is unreachable at startup.

`SlidingWindowRateLimitMiddleware` is now async-aware and awaits
`check_request()`.

### OpenTelemetry FastAPI auto-instrumentation

When `AGENT_OTEL_ENABLED=true`, the lifespan calls
`FastAPIInstrumentor.instrument_app(app)` after `init_otel_exporter()`.
Every request emits a span named after the matched route
(`POST /api/tasks`, `GET /api/metrics`, etc.) for per-route
latency dashboards in Jaeger / Tempo / SigNoz. Idempotent;
gracefully degrades if `opentelemetry-instrumentation-fastapi`
isn't installed.

### PostgreSQL row-level security (RLS)

Tenant isolation now enforced at the database schema level, not
just the API layer:

- `graph_nodes` / `graph_links` gained `tenant_id` columns with
  indexes; RLS policies filter via
  `current_setting('app.current_tenant', true)`.
- `PostgresBackend.set_tenant_id()` validates input (1-128 chars);
  `_conn_with_tenant()` emits `set_config(..., true)` per checkout
  (LOCAL = auto-expires at transaction end, no tenant leak
  between pool checkouts).
- Migration (`RLS_MIGRATION_SQL`) is idempotent — `ADD COLUMN
  IF NOT EXISTS` + `DROP POLICY IF EXISTS` + `CREATE POLICY`
  pattern, safe to re-run on every `init()`.
- Fail-closed: unset GUC → zero rows visible.
- Cross-tenant admin: connect as a role with `BYPASSRLS` attribute.

### WebSocket streaming LLM

`GET /api/ws/llm/stream?token=&prompt=&system=` upgrades a
WebSocket and emits text deltas as the LLM produces them.
`LLMRouter.stream_chunks()` async generator works with both
Anthropic (`messages.stream`) and OpenAI-compatible
(`chat.completions stream=True`). 15-second keepalive pings;
cancels the LLM generator on client disconnect.

Wire format:
```json
{"type": "chunk", "data": "..."}
{"type": "done",  "data": {"input_tokens": 0, "output_tokens": 0, "duration_ms": 0, "model": "..."}}
{"type": "error", "data": "..."}
{"type": "ping"}
```

### Test coverage

920 collected → **992 collected** (+72):
- 22 RS256 + JWKS tests
- 21 Redis backend tests
- 5 OTel FastAPI instrumentation tests
- 15 RLS tests
- 2 router-level stream tests (5 WS endpoint tests skipped due to
  starlette 1.3.x + httpx 0.28 TestClient incompatibility)

### Known limitations (unchanged from v0.1.x)

- HS256 path still supported; RS256 is the recommended default for
  new deployments.
- Rate limit at scale requires Redis (single-replica deploys are
  fine with the in-memory backend).
- PostgreSQL RLS only takes effect with the PostgresBackend —
  SQLite deployments still rely on API-layer tenant filtering.

### Upgrade

Drop-in replacement for v0.1.1. No config or migration steps
required. New env vars are all optional; existing HS256 deployments
continue to work without any change.

---

## v0.3.0 — 2026-07-22 (GitHub App integration — roadmap pull-forward)

**Git tag:** `v0.3.0` (commit `6108be5d`)
**Scope:** new feature; backward compatible.

This release delivers the v0.3.0 roadmap item "GitHub App
integration" on top of v0.2.0. Register the server as a GitHub
App and webhooks trigger `ReviewAgent` automatically when a PR
is opened, synchronized, or reopened. The review runs in the
background so the webhook responds within GitHub's 10s timeout.

### Added

- **`POST /api/webhooks/github`** — GitHub App webhook receiver.
  HMAC-SHA256 signature verification via `X-Hub-Signature-256`
  (constant-time `hmac.compare_digest`). Raw body is read before
  JSON parsing so the byte sequence matches what GitHub signed.
  - Replay protection via `X-GitHub-Delivery` cache (LRU 1000).
    Duplicate deliveries return `{"status": "duplicate"}`.
  - Other event types acknowledged with `{"status": "ignored"}`
    so GitHub doesn't retry.
  - Background dispatch via `asyncio.create_task` keeps webhook
    latency under 10s even if the LLM call is slow.
  - Opt-in comment posting via `GITHUB_PR_COMMENT_TOKEN`; without
    it, review output is logged locally (staging-friendly).
  - `GITHUB_WEBHOOK_SECRET` required; 503 if unset, so
    misconfiguration is loud, not silent.
- **Custom Agent marketplace** — YAML-defined agents per tenant.
  - `load_from_yaml_file()` / `load_from_directory()` in
    `agent_system.agents.custom.loader` parse + validate YAML
    into `CustomAgentConfig`.
  - HTTP API at `/api/custom-agents` (5 endpoints):
    - `GET    /api/custom-agents` — list (tenant-scoped)
    - `GET    /api/custom-agents/{id}` — detail
    - `POST   /api/custom-agents/{id}/run` — invoke
    - `POST   /api/custom-agents:upload` — admin-only YAML upload
    - `DELETE /api/custom-agents/{id}` — admin-only remove
  - Multi-tenant isolation: all endpoints scope by JWT
    `tenant_id` claim. Cross-tenant access returns 404 (no info
    leak about other tenants' agent IDs).
  - Two example configs in `examples/custom-agents/`:
    - `translator.yaml` — text-only translation agent
    - `pr-summarizer.yaml` — uses `read_file` + `code_search`

### Documentation

- 4 ADRs added to `docs/adr/`:
  - `0000` template
  - `0001` RS256 JWT (multi-issuer / external verifiers)
  - `0002` PostgreSQL RLS (defense in depth)
  - `0003` GitHub webhook (self-host vs tunnel)
- `examples/custom-agents/`: ready-to-deploy YAML examples.

### Tests

18 new in `test_github_webhook.py` (signature + replay + event
dispatch) + 19 new in `test_custom_agent_loader.py` (YAML parse +
validation + 5-endpoint HTTP API + tenant isolation).

### Upgrade

Drop-in replacement for v0.2.0. No config or migration steps.
New env vars are all optional: `GITHUB_WEBHOOK_SECRET` (required
only if you want the webhook to work — 503 otherwise),
`GITHUB_PR_COMMENT_TOKEN` (optional, for PR comments),
`AGENT_CUSTOM_AGENTS_DIR` (optional, defaults to `<tmp>/agent_custom_agents/`).

---

## Test count summary

| Version | Tests | Skip | xfail | Known failures |
|---|---|---|---|---|
| v0.1.0 | 367 | — | — | — |
| v0.1.1 | 910 | 7 | 2 | 1 (real-LLM gated) |
| v0.2.0 | 992 | 5 | 2 | 3 (real-LLM gated) |
| v0.3.0 | 1012 | 5 | 2 | 3 (real-LLM gated) |