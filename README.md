# Agent System

[![CI](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![v0.5.0](https://img.shields.io/badge/release-v0.5.0-blue)](https://github.com/liwt2010/all-agents/releases/tag/v0.5.0)

**Enterprise Multi-Agent Orchestration Platform** — production-grade AI agent system with shared memory, schema tolerance, data provenance, distributed tracing, OpenAPI/SDK auto-generation, end-to-end observability, RS256 JWT auth, Redis-backed rate limiting, PostgreSQL row-level security, WebSocket LLM streaming, GitHub App integration, and a Custom Agent marketplace.

```
User → CEO Agent → Product Agent → Tech Agent → Test Agent → Deploy Agent
                    ↘ Security    ↘ Docs      ↘ Review       ↘ DevOps
```

---

## Current status (v0.5.0)

- **1048** tests pass, **5** skipped (WebSocket TestClient framework limitation, documented), **2** xfail (openapi-python-client upstream bug)
- **3** known failures in `test_*real_llm.py` — all skip without `ANTHROPIC_API_KEY`; pass in CI with a key
- Latest tags: `v0.1.0`, `v0.1.1`, `v0.2.0`, `v0.3.0`, `v0.4.0`, `v0.5.0` — see [RELEASE_NOTES.md](RELEASE_NOTES.md)

---

## Why Agent System?

| Single AI | Agent System |
|---|---|
| One-shot answer | Multi-step pipeline with peer review |
| Single context window | Shared memory graph (11 node types, 23 link types) |
| Manual tool switching | Auto-discovered MCP tool registry |
| No audit trail | Full audit log + LLM cost tracking |
| Silent failures | Data provenance labels (REAL_LLM / MOCK / LLM_FAILURE) |
| Opaque to ops | Prometheus + per-route OpenTelemetry spans |
| Single-secret JWT | RS256 + JWKS endpoint, multi-issuer safe |
| Single-replica | Distributed sliding-window rate limit (Redis Lua) |
| Schema-level leak risk | PostgreSQL row-level security (RLS) |
| One-shot LLM calls | WebSocket token-by-token streaming |
| Manual code review | GitHub App auto-triggers ReviewAgent on PR |
| Source-only agents | YAML-defined custom agents per tenant |
| Hard to extend | OpenAPI/SDK auto-gen + ADR-documented decisions |

---

## Quick Start

```bash
# 1. Install
pip install -e ".[api,storage]"

# 2. Configure
export ANTHROPIC_API_KEY=sk-xxx           # or OPENAI_API_KEY
export AUTH_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))")

# 3. Run a single agent
python -m agent_system run "Write a PRD for a login feature"

# 4. Run the full pipeline
python -m agent_system pipeline "Build a todo app"

# 5. Start the API server
uvicorn agent_system.api.server:app --host 0.0.0.0 --port 8000
```

Or via Docker:

```bash
docker run -d --name agent-system \
  -e AUTH_PRIVATE_KEY="$(cat /path/to/private.pem)" \
  -e AUTH_PUBLIC_KEYS="v1:$(cat /path/to/public.pem)" \
  -e ANTHROPIC_API_KEY=sk-xxx \
  -e REDIS_URL=redis://host:6379 \
  -p 8000:8000 \
  liwt2010/all-agents:v0.5.0
```

Generate the RSA keypair once with: `python scripts/gen_rsa_keys.py --kid v1`

Browse the API:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc
- OpenAPI JSON: http://localhost:8000/openapi.json
- Health: http://localhost:8000/api/health
- Metrics: http://localhost:8000/metrics

---

## Agents

| Agent | Role | Key Capabilities |
|-------|------|-----------------|
| **CEO** | Orchestrator | Task dispatch, 4-way escalation, pipeline management |
| **Product** | Requirements | PRD writing, feature breakdown, acceptance criteria |
| **Tech** | Implementation | Code generation, architecture design, code review |
| **Test** | Quality | Test generation, execution, coverage analysis |
| **Deploy** | Operations | Staging/prod rollout, migration, rollback |
| **Security** | Compliance | Secrets scanning, CVE, threat modeling |
| **Docs** | Documentation | API reference, runbooks, ADRs, changelogs |
| **Review** | Peer Review | Code/design/test plan review, merge approval |
| **DevOps** | Infrastructure | CI/CD, K8s, monitoring, IaC review |

---

## Production Features

### v0.3.0 — Custom Agent Marketplace + GitHub App

- **YAML-defined custom agents** — tenants define their own agents via
  `examples/custom-agents/*.yaml`, no code change required.
  Loaded by `load_from_directory()` and exposed via
  `/api/custom-agents` (list / get / run / upload / delete).
  Multi-tenant scoped; cross-tenant access returns 404.
- **GitHub App webhook integration** — `POST /api/webhooks/github`
  verifies HMAC-SHA256 signatures, deduplicates replays by
  `X-GitHub-Delivery`, and triggers `ReviewAgent` on
  `pull_request` opened/synchronize/reopened. Optional
  `GITHUB_PR_COMMENT_TOKEN` posts the review back as a PR comment.

### v0.2.0 — Production hardening

#### RS256 JWT + JWKS endpoint

`AuthService` auto-detects: `AUTH_PRIVATE_KEY` → RS256 (asymmetric,
recommended for multi-issuer/multi-tenant); otherwise HS256 (legacy).
Public keys distributed via `GET /api/auth/jwks` (RFC 7517) so
external services can verify tokens locally. `scripts/gen_rsa_keys.py`
generates 2048/3072/4096-bit keypairs with `--env-file` mode that
writes the config directly.

#### Distributed sliding-window rate limit

Pluggable `RateLimiterBackend`:
- `InMemoryBackend` — default, single-process
- `RedisBackend` — multi-replica safe via Lua atomic
  check-and-record on a ZSET. Activated by setting `REDIS_URL`.
  Falls back to in-memory if Redis is unreachable at startup.

`SlidingWindowRateLimitMiddleware` is now async-aware and awaits
`check_request()`.

#### OpenTelemetry FastAPI auto-instrumentation

When `AGENT_OTEL_ENABLED=true`, the lifespan calls
`FastAPIInstrumentor.instrument_app(app)` after `init_otel_exporter()`.
Every request emits a span named after the matched route
(`POST /api/tasks`, `GET /api/metrics`, etc.) for per-route
latency dashboards in Jaeger / Tempo / SigNoz. Idempotent;
gracefully degrades if `opentelemetry-instrumentation-fastapi`
isn't installed.

#### PostgreSQL row-level security (RLS)

Tenant isolation enforced at the database schema level, not just
the API layer. `RLS_MIGRATION_SQL` (idempotent, safe to re-run
on every `init()`) adds:
- `tenant_id` columns + indexes on `graph_nodes` and `graph_links`
- `ENABLE ROW LEVEL SECURITY` on both tables
- `CREATE POLICY ... USING (tenant_id = current_setting('app.current_tenant', true))`
- Fail-closed: unset GUC → zero rows visible
- `PostgresBackend.set_tenant_id()` validates input (1-128 chars);
  `_conn_with_tenant()` emits `set_config(..., true)` per checkout
  (LOCAL → auto-expires at transaction end, no tenant leak
  between pool checkouts)
- Cross-tenant admin via `BYPASSRLS` role

#### WebSocket streaming LLM

`GET /api/ws/llm/stream?token=&prompt=&system=` upgrades a
WebSocket and emits text deltas as the LLM produces them.
`LLMRouter.stream_chunks()` async generator works with both
Anthropic (`messages.stream`) and OpenAI-compatible
(`chat.completions stream=True`). 15-second keepalive pings;
cancels the LLM generator on client disconnect.
Wire format: `{"type":"chunk","data":"..."}`,
`{"type":"done","data":{usage...}}`, `{"type":"error",...}`,
`{"type":"ping"}`.

### v0.1.0 — Initial production release

#### Core platform

- **9 built-in agents** (5 production + 4 specialized)
- **SmartAgent.execute()** split into checkpoint / retry / failure / escalate
- **Dataview engine** — SQL-like query over the memory graph
- **4-way resolver**: SELF / PEER / HUMAN / ESCALATE
- **AgentRegistry** for dynamic agent lookup
- **Custom Agent platform** — Pydantic v2-friendly, hot-reloadable

#### Memory & learning

- **MultiLinkGraph** — 11 node types, 23 link types, time-decayed similarity
- **Experience feedback loop** — failed tasks inform future attempts
- **`memory_enabled` opt-out** for ephemeral workflows

#### Schema & data integrity

- **4-tier schema tolerance** (STRICT / LENIENT / REPAIR / WARN) with auto-repair
- **Data provenance** on every output: `REAL_LLM` (conf 0.85) / `MOCK` (0.0) / `LLM_FAILURE` (0.0)
- **FailureNodeLogger** — every LLM failure becomes an auditable graph node
- **`raw_output` fallback** — partial results never silently fail

#### Observability

- **OpenTelemetry distributed tracing** — DISABLED / CONSOLE / OTLP_HTTP modes
  - `agent.execute` span with status + exception on error
  - FastAPI middleware auto-wraps every HTTP request
- **Prometheus metrics** — 11 metrics at `/metrics`
- **Batch audit logger** with retention (90d default) + HTTP query endpoint
- **Request ID propagation** via `X-Request-ID` header

#### API & SDK

- **OpenAPI 3.1** spec with rich metadata (3 servers, 7 tags, 9 schemas)
- **Python SDK** auto-generated via `openapi-python-client`
- **TypeScript SDK** auto-generated via `openapi-typescript-codegen`
- **`make codegen`** for one-command regeneration

#### Security hardening

- **CORS** — environment-aware, denies `*` in production, enforces `https://`
- **TLS** — HSTS header (on in production), HTTPS redirect middleware, secure-cookie checker
- **JWT secret rotation** — `AUTH_SECRETS="kid:secret,..."` multi-key with no-downtime rollover
- **Sliding-window rate limit** — per-user + per-scope
- **Request size cap** (1MB default) + **secrets-in-request** detection
- **Input sanitizer** — prompt-injection detection (TrustLevel-aware)

#### Storage & ops

- **Pluggable storage** — JSON / SQLite / PostgreSQL
- **Backup subsystem** — cron + SHA-256 manifest + tar.gz + DR drill
- **Distributed lock** — Redis-backed with in-memory fallback
- **Migration CLI** — switch backends without data loss
- **Multi-tenant isolation** — 6-space isolation model + PostgreSQL RLS
- **RBAC** — 6 roles, 7 permissions, permission group overrides

### Architecture decisions

Major decisions are recorded as ADRs in [`docs/adr/`](docs/adr/) —
[RS256 JWT](docs/adr/0001-rs256-jwt.md),
[PostgreSQL RLS](docs/adr/0002-postgres-rls.md),
[GitHub webhook](docs/adr/0003-github-webhook.md).
Use these to understand *why* the code looks the way it does,
not just *what* it does.

---

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/health` | GET | No | Liveness probe |
| `/api/ready` | GET | No | Readiness probe (checks DB, LLM) |
| `/api/auth/token` | POST | No | Issue JWT (dev only; RS256 in v0.2.0) |
| `/api/auth/jwks` | GET | No | RS256 public keys (RFC 7517 JWKS) |
| `/api/agents` | GET | JWT | List available agents |
| `/api/tasks` | POST | JWT | Submit a task |
| `/api/tasks/{id}` | GET | JWT | Get task result |
| `/api/tasks` | GET | JWT | List tasks (paginated, tenant-isolated) |
| `/api/tasks/{id}/progress` | GET | JWT | Live progress |
| `/api/graph/stats` | GET | JWT | Graph statistics |
| `/api/graph/node/{id}` | GET | JWT | Get a specific graph node |
| `/api/audit/query` | GET | JWT | Query audit log |
| `/api/metrics` | GET | JWT | Application metrics (JSON) |
| `/api/custom-agents` | GET | JWT | List custom agents (tenant-scoped) |
| `/api/custom-agents/{id}` | GET | JWT | Custom agent detail |
| `/api/custom-agents/{id}/run` | POST | JWT | Invoke a custom agent |
| `/api/custom-agents:upload` | POST | JWT (admin) | Register a YAML agent |
| `/api/custom-agents/{id}` | DELETE | JWT (admin) | Remove a custom agent |
| `/api/ws/llm/stream` | WS | JWT (query) | Streaming LLM tokens |
| `/api/webhooks/github` | POST | HMAC | GitHub App webhook receiver |
| `/metrics` | GET | No | Prometheus scrape endpoint |

Full OpenAPI spec: [/openapi.json](http://localhost:8000/openapi.json)

---

## Configuration

| Env Var | Required | Default | Description |
|---------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes* | — | LLM API key (Anthropic / OpenAI-compatible) |
| `AUTH_PRIVATE_KEY` | RS256 only | — | RSA private key (PEM, PKCS#8) — signs new tokens |
| `AUTH_PUBLIC_KEYS` | RS256 multi-issuer | — | Comma-separated `kid:public_pem` — verify keys |
| `AUTH_SIGNING_KID` | Optional | `current` | kid for the current private key |
| `AUTH_SECRET` | HS256 only | — | JWT signing secret (32+ chars) — or `AUTH_SECRETS` for rotation |
| `AUTH_SECRETS` | HS256 only | — | `kid:secret,kid:secret,...` for rotation |
| `ENVIRONMENT` | No | `development` | `production` enables strict mode |
| `LLM_PROVIDER` | No | `anthropic` | `anthropic` / `openai` / `mock` |
| `LLM_MODEL` | No | (provider default) | Model name (e.g. `claude-haiku-4-5-20251001`) |
| `CORS_ALLOWED_ORIGINS` | Prod yes | localhost:5173 (dev) | Comma-separated https:// origins |
| `AGENT_OTEL_ENABLED` | No | `false` | Enable OpenTelemetry tracing |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | If OTEL on | `http://localhost:4318` | OTLP/HTTP collector URL |
| `STORAGE_BACKEND` | No | `json` | `json` / `sqlite` / `postgres` |
| `POSTGRES_URL` | If postgres | — | PostgreSQL connection string |
| `REDIS_URL` | No | — | Redis URL (for distributed rate limit / lock) |
| `AGENT_RATE_LIMIT_SCOPE_DEFAULT_USER` | No | 120 | Default scope user limit per minute |
| `AGENT_RATE_LIMIT_SCOPE_DEFAULT_IP` | No | 240 | Default scope IP limit per minute |
| `AGENT_RATE_LIMIT_SCOPE_EXPENSIVE_USER` | No | 20 | LLM-calling endpoints per user |
| `AGENT_RATE_LIMIT_SCOPE_AUTH_USER` | No | 5 | Auth endpoints (anti-brute-force) |
| `AGENT_RATE_LIMIT_ENABLED` | No | `true` | Toggle sliding-window rate limit |
| `AGENT_AUDIT_RETENTION_DAYS` | No | 90 | Audit log retention |
| `AGENT_BACKUP_CRON` | No | `0 2 * * *` | Backup schedule (cron) |
| `AGENT_CUSTOM_AGENTS_DIR` | No | `<tmp>/agent_custom_agents/` | Where custom agent YAML is stored |
| `GITHUB_WEBHOOK_SECRET` | Webhook only | — | HMAC secret for GitHub webhook signature |
| `GITHUB_PR_COMMENT_TOKEN` | Optional | — | PAT for posting PR review comments |
| `TLS_REDIRECT_ENABLED` | No | `false` | Enable HTTP→HTTPS 301 |
| `TLS_HSTS_ENABLED` | No | `true` (prod) | Add HSTS header |
| `MAX_REQUEST_BYTES` | No | `1048576` | Request body cap (1MB) |
| `ALLOWED_FILE_ROOTS` | No | `data,tmp` | File sandbox roots |

*For dev with `mock` provider, no key needed.

See [.env.example](.env.example) for the full annotated list.

---

## Project Structure

```
src/agent_system/
├── agents/             # 9 built-in agents + custom/ subpackage
│   ├── product_agent.py / tech_agent.py / test_agent.py / ...
│   └── custom/         # YAML-defined custom agents (loader + base + registry)
├── api/                # FastAPI server (routers/, state.py, server.py)
│   └── routes/         # health / auth / tasks / agents / graph / metrics /
│                       # audit / llm_stream / github_webhook / custom_agents
├── auth/               # (deprecated — moved to core/auth/)
├── codegen/            # OpenAPI spec dump + Python/TypeScript SDK gen
├── concurrency/        # Distributed lock (Redis + in-memory fallback)
├── config/             # ConfigManager (4-layer override)
├── core/               # SmartAgent, LLM router, security middleware, audit
│   ├── agent.py        # SmartAgent + TaskContext + OutputSchema
│   ├── auth/           # JWT (HS256+RS256), RBAC, TenantContext
│   ├── llm_router.py   # Multi-provider + streaming (Anthropic + OpenAI)
│   ├── observability/  # DataProvenance + tracing + cost tracking
│   ├── rate_limit/     # SlidingWindowLimiter + pluggable Backend
│   ├── backup/         # Manifest + scheduler + restore + retention
│   ├── security/       # CORS + TLS + secret rotation
│   └── ...
├── memory/             # MultiLinkGraph, experience feedback loop, embeddings
│   └── storage/        # JSON / SQLite / PostgreSQL + migration
├── observability/      # Prometheus metrics + OTel exporter + middleware
├── storage/            # JSON / SQLite / PostgreSQL backends + migration CLI
├── tools/              # Plugin tool system + MCP client
├── migration/          # Data migration engine
└── onboarding/         # First-time user experience

docs/
├── PRODUCTION.md       # 11-section deployment guide
├── RUNBOOK.md          # Incident response
├── STORAGE.md          # Storage backends + PostgreSQL RLS (v0.2.0)
├── RATE_LIMIT.md       # Rate limiter + Redis backend (v0.2.0)
├── METRICS.md          # Prometheus metrics
├── BACKUP.md           # Backup + DR drill
├── AUDIT.md            # Audit log
├── DATAVIEW.md         # Memory graph query engine
├── CUSTOM_AGENT.md     # Custom Agent design
└── adr/                # Architectural Decision Records
    ├── 0000-adr-template.md
    ├── 0001-rs256-jwt.md
    ├── 0002-postgres-rls.md
    └── 0003-github-webhook.md

examples/
└── custom-agents/      # Example YAML for the marketplace
    ├── translator.yaml
    └── pr-summarizer.yaml

sdks/
└── python/             # Auto-generated Python SDK (make codegen)
```

---

## Using the SDKs

### Python

```python
from agent_system_api_client import Client
from agent_system_api_client.api.default import health_api_health_get

client = Client(base_url="https://api.example.com")
response = health_api_health_get.sync(client=client)
print(response)
```

### TypeScript

```typescript
import { Configuration, DefaultApi } from 'agent-system-client';

const config = new Configuration({ basePath: 'https://api.example.com' });
const api = new DefaultApi(config);
const health = await api.healthApiHealthGet();
console.log(health);
```

### Regenerate SDKs

```bash
make codegen      # OpenAPI spec + Python SDK
make codegen-ts   # also TypeScript SDK (needs Node.js)
```

---

## Testing

```bash
# Unit tests (always run, no LLM required)
pytest tests/ -q --ignore=tests/test_*real_llm.py

# Real-LLM E2E tests (need API key — most are skipped without one)
ANTHROPIC_API_KEY=sk-xxx pytest tests/test_*real_llm.py -v

# Production-readiness gate (always runs in CI)
pytest tests/test_production_readiness.py -v

# RS256 JWT tests (auto-skipped if cryptography missing)
pytest tests/test_auth_rs256.py -v

# Redis rate-limit tests (needs fakeredis in dev deps)
pytest tests/test_rate_limit_redis.py -v

# WebSocket streaming tests (router-level passes; endpoint-level
# is skipped due to anyio 4.x + httpx 0.28 TestClient transport bug)
pytest tests/test_llm_stream.py -v

# GitHub webhook tests
pytest tests/test_github_webhook.py -v

# Custom Agent marketplace tests
pytest tests/test_custom_agent_loader.py -v
```

**Current state** (v0.3.0): **1012** tests pass, **5** skipped (WebSocket
endpoint-level — documented framework limitation), **2** xfail
(`openapi-python-client` 0.26 upstream UP007 bug), **3** known failures
in `test_*real_llm.py` (all skip locally without `ANTHROPIC_API_KEY`,
pass in CI with a key), 0 known regressions.

---

## Production Deployment

See [docs/PRODUCTION.md](docs/PRODUCTION.md) for the full 13-section
guide covering:

1. Pre-deployment checklist
2. Environment variables (4 categories)
3. LLM API key handling
4. Storage backend selection
5. Container deployment (Docker + K8s ingress-nginx)
6. Health & readiness probes
7. Monitoring (Prometheus + OTel + audit log)
8. Backups & DR
9. Performance targets
10. Security (CORS, TLS, JWT rotation, RS256 vs HS256)
11. CI/CD gate
12. Incident response
13. Contacts + versioning

Incident response: [docs/RUNBOOK.md](docs/RUNBOOK.md)

---

## Roadmap

### v0.3.0 — delivered ✅

- ✅ **Custom Agent Marketplace** — YAML-defined agents per tenant,
  HTTP API for list/get/run/upload/delete
- ✅ **GitHub App integration** — webhook receiver + auto PR review
  dispatch via ReviewAgent

### v0.2.0 — delivered ✅

- ✅ **RS256 JWT** with JWKS endpoint and `gen_rsa_keys.py`
- ✅ **Redis-backed rate limit** via Lua-atomic sliding window
- ✅ **PostgreSQL row-level security** for tenant isolation at
  schema level
- ✅ **OpenTelemetry FastAPI auto-instrumentation** for per-route
  spans
- ✅ **WebSocket streaming LLM** with token-by-token delivery

### Forward-looking (post-v0.3.0)

- **Streaming LLM completion events** (function-call streaming,
  tool-call streaming) — currently only text deltas
- **Multi-tenant Custom Agent marketplace UI** — web frontend for
  browsing/upload of custom agents
- **HL7 / FHIR adapter** — healthcare data format integration
- **Native gRPC server** alongside the REST/WS API
- **Distributed task queue** — currently single-process execution;
  add Celery/RQ for high-throughput deployments

## License

MIT — see [LICENSE](LICENSE).

---

## Release History

- **v0.3.0** (2026-07-22) — Custom Agent marketplace + GitHub App
  - YAML-defined custom agents per tenant (5 endpoints, 2 examples)
  - GitHub webhook receiver with HMAC + replay protection
  - See [RELEASE_NOTES.md](RELEASE_NOTES.md)
- **v0.2.0** (2026-07-22) — Production-hardening milestone
  - RS256 JWT + JWKS endpoint
  - Redis-backed rate limiting
  - PostgreSQL row-level security
  - OpenTelemetry FastAPI auto-instrumentation
  - WebSocket streaming LLM
  - 6 new tests per PR + 6 ADRs in `docs/adr/`
  - See [RELEASE_NOTES.md](RELEASE_NOTES.md)
- **v0.1.1** (2026-07-22) — Bug fixes + typing sweep
  - 11 real test failures fixed, 84 files typing-modernized
- **v0.1.0** (2026-07-09) — First production-grade release
  - 22 PRs delivered, 367 tests passing
  - OpenAPI + Python/TypeScript SDK auto-generation
  - OpenTelemetry distributed tracing
  - CORS / TLS / JWT secret rotation hardening

See [RELEASE_NOTES.md](RELEASE_NOTES.md) for the full breakdown.
