# Agent System

[![CI](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml/badge.svg)](https://github.com/liwt2010/all-agents/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![v0.1.0](https://img.shields.io/badge/release-v0.1.0-blue)](https://github.com/liwt2010/all-agents/releases/tag/v0.1.0)

**Enterprise Multi-Agent Orchestration Platform** — production-grade AI agent system with shared memory, schema tolerance, data provenance, distributed tracing, OpenAPI/SDK auto-generation, and end-to-end observability.

```
User → CEO Agent → Product Agent → Tech Agent → Test Agent → Deploy Agent
                    ↘ Security    ↘ Docs      ↘ Review       ↘ DevOps
```

---

## Why Agent System?

| Single AI | Agent System |
|---|---|
| One-shot answer | Multi-step pipeline with peer review |
| Single context window | Shared memory graph (11 node types, 23 link types) |
| Manual tool switching | Auto-discovered MCP tool registry |
| No audit trail | Full audit log + LLM cost tracking |
| Silent failures | Data provenance labels (REAL_LLM / MOCK / LLM_FAILURE) |
| Opaque to ops | Prometheus + OpenTelemetry out of the box |
| Hard to extend | Custom Agent platform + OpenAPI/SDK auto-gen |

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
  -e AUTH_SECRET=$(python -c "import secrets; print(secrets.token_urlsafe(48))") \
  -e ANTHROPIC_API_KEY=sk-xxx \
  -p 8000:8000 \
  liwt2010/all-agents:v0.1.0
```

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

## Production Features (v0.1.0)

### Core platform
- **9 built-in agents** (5 production + 4 specialized)
- **SmartAgent.execute()** split into checkpoint / retry / failure / escalate
- **Dataview engine** — SQL-like query over the memory graph
- **4-way resolver**: SELF / PEER / HUMAN / ESCALATE
- **AgentRegistry** for dynamic agent lookup
- **Custom Agent platform** — Pydantic v2-friendly, hot-reloadable

### Memory & learning
- **MultiLinkGraph** — 11 node types, 23 link types, time-decayed similarity
- **Experience feedback loop** — failed tasks inform future attempts
- **`memory_enabled` opt-out** for ephemeral workflows

### Schema & data integrity
- **4-tier schema tolerance** (STRICT / LENIENT / REPAIR / WARN) with auto-repair
- **Data provenance** on every output: `REAL_LLM` (conf 0.85) / `MOCK` (0.0) / `LLM_FAILURE` (0.0)
- **FailureNodeLogger** — every LLM failure becomes an auditable graph node
- **`raw_output` fallback** — partial results never silently fail

### Observability
- **OpenTelemetry distributed tracing** — DISABLED / CONSOLE / OTLP_HTTP modes
  - `agent.execute` span with status + exception on error
  - FastAPI middleware auto-wraps every HTTP request
- **Prometheus metrics** — 11 metrics at `/metrics`
- **Batch audit logger** with retention (90d default) + HTTP query endpoint
- **Request ID propagation** via `X-Request-ID` header

### API & SDK
- **OpenAPI 3.1** spec with rich metadata (3 servers, 7 tags, 9 schemas)
- **Python SDK** auto-generated via `openapi-python-client`
- **TypeScript SDK** auto-generated via `openapi-typescript-codegen`
- **`make codegen`** for one-command regeneration

### Security hardening
- **CORS** — environment-aware, denies `*` in production, enforces `https://`
- **TLS** — HSTS header (on in production), HTTPS redirect middleware, secure-cookie checker
- **JWT secret rotation** — `AUTH_SECRETS="kid:secret,..."` multi-key with no-downtime rollover
- **Sliding-window rate limit** — per-user + per-scope
- **Request size cap** (1MB default) + **secrets-in-request** detection
- **Input sanitizer** — prompt-injection detection (TrustLevel-aware)

### Storage & ops
- **Pluggable storage** — JSON / SQLite / PostgreSQL
- **Backup subsystem** — cron + SHA-256 manifest + tar.gz + DR drill
- **Distributed lock** — Redis-backed with in-memory fallback
- **Migration CLI** — switch backends without data loss
- **Multi-tenant isolation** — 6-space isolation model
- **RBAC** — 6 roles, 7 permissions, permission group overrides

### Developer experience
- **Production deployment guide** — [docs/PRODUCTION.md](docs/PRODUCTION.md) (11KB, 15 sections)
- **Incident response runbook** — [docs/RUNBOOK.md](docs/RUNBOOK.md)
- **Release notes** — [RELEASE_NOTES.md](RELEASE_NOTES.md)
- **CI gate** — production-readiness test suite blocks low-quality PRs

---

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/health` | GET | No | Liveness probe |
| `/api/ready` | GET | No | Readiness probe (checks DB, LLM) |
| `/api/auth/token` | POST | No | Issue JWT (dev only — RS256 in v0.2.0) |
| `/api/agents` | GET | JWT | List available agents |
| `/api/tasks` | POST | JWT | Submit a task |
| `/api/tasks/{id}` | GET | JWT | Get task result |
| `/api/tasks` | GET | JWT | List tasks (paginated, tenant-isolated) |
| `/api/tasks/{id}/progress` | GET | JWT | Live progress |
| `/api/graph/stats` | GET | JWT | Graph statistics |
| `/api/graph/node/{id}` | GET | JWT | Get a specific graph node |
| `/api/audit/query` | GET | JWT | Query audit log |
| `/api/metrics` | GET | JWT | Application metrics (JSON) |
| `/metrics` | GET | No | Prometheus scrape endpoint |

Full OpenAPI spec: [/openapi.json](http://localhost:8000/openapi.json)

---

## Configuration

| Env Var | Required | Default | Description |
|---------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes* | — | LLM API key (Anthropic / OpenAI-compatible) |
| `AUTH_SECRET` | Yes | — | JWT signing secret (32+ chars) — or `AUTH_SECRETS` for rotation |
| `ENVIRONMENT` | No | `development` | `production` enables strict mode |
| `LLM_PROVIDER` | No | `anthropic` | `anthropic` / `openai` / `mock` |
| `LLM_MODEL` | No | (provider default) | Model name (e.g. `claude-3-5-sonnet`) |
| `CORS_ALLOWED_ORIGINS` | Prod yes | localhost:5173 (dev) | Comma-separated https:// origins |
| `AGENT_OTEL_ENABLED` | No | `false` | Enable OpenTelemetry tracing |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | If OTEL on | `http://localhost:4318` | OTLP/HTTP collector URL |
| `STORAGE_BACKEND` | No | `json` | `json` / `sqlite` / `postgres` |
| `POSTGRES_URL` | If postgres | — | PostgreSQL connection string |
| `REDIS_URL` | No | — | Redis URL (for distributed lock) |
| `RATE_LIMIT_PER_MINUTE` | No | 120 | Per-user requests/minute (default scope) |
| `AGENT_RATE_LIMIT_ENABLED` | No | `true` | Toggle sliding-window rate limit |
| `AGENT_AUDIT_RETENTION_DAYS` | No | 90 | Audit log retention |
| `AGENT_BACKUP_CRON` | No | `0 2 * * *` | Backup schedule (cron) |
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
├── agents/          # 9 built-in agents (Product, Tech, Test, Deploy, CEO, Security, Docs, Review, DevOps)
├── api/             # FastAPI server (OpenAPI, middleware, WebSocket)
├── auth/            # JWT + RBAC + multi-tenant context (re-exported from core/auth/)
├── codegen/         # OpenAPI spec dump + Python/TypeScript SDK generator (PR-15)
├── concurrency/     # Distributed lock (Redis + in-memory fallback)
├── config/          # ConfigManager (4-layer override)
├── core/            # SmartAgent, LLM router, security middleware, audit
│   ├── auth/        # JWT, RBAC, TenantContext
│   ├── observability/  # DataProvenance, tracing
│   ├── rate_limit/  # SlidingWindowLimiter + LimiterRegistry (PR-12)
│   ├── backup/      # Manifest + scheduler + restore + retention (PR-13)
│   ├── security/    # CORS + TLS + secret rotation (PR-16)
│   └── ...
├── memory/          # MultiLinkGraph, experience feedback loop, embeddings
├── observability/   # Prometheus metrics, OTel exporter + middleware (PR-14)
├── storage/         # JSON / SQLite / PostgreSQL backends + migration CLI
├── tools/           # Plugin tool system + MCP client
├── migration/       # Data migration engine
└── onboarding/      # First-time user experience
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

# Real-LLM E2E tests (need API key)
ANTHROPIC_API_KEY=sk-xxx pytest tests/test_*real_llm.py -v

# Production-readiness gate (always runs in CI)
pytest tests/test_production_readiness.py -v
```

**Current state**: 362 unit tests + 5 real-LLM E2E tests, 0 known regressions.

---

## Production Deployment

See [docs/PRODUCTION.md](docs/PRODUCTION.md) for the full 11KB guide covering:

1. Pre-deployment checklist
2. Environment variables (4 categories)
3. LLM API key handling
4. Storage backend selection
5. Container deployment (Docker + K8s ingress-nginx)
6. Health & readiness probes
7. Monitoring (Prometheus + OTel + audit log)
8. Backups & DR
9. Performance targets
10. Security (CORS, TLS, JWT rotation)
11. CI/CD gate
12. Incident response
13. Contacts
14. Versioning

Incident response: [docs/RUNBOOK.md](docs/RUNBOOK.md)

---

## Roadmap (v0.2.0+)

- [ ] **RS256 JWT** (multi-issuer / multi-tenant at scale)
- [ ] **Redis-backed rate limit** (multi-replica)
- [ ] **PostgreSQL row-level security** (per-tenant isolation at schema level)
- [ ] **OpenTelemetry FastAPI auto-instrumentation** (per-route granularity)
- [ ] **Streaming LLM responses** via WebSocket
- [ ] **GitHub App integration** (auto PR review)
- [ ] **Custom Agent marketplace** (shareable templates)

---

## License

MIT — see [LICENSE](LICENSE).

---

## Release History

- **v0.1.0** (2026-07-09) — First production-grade release
  - 22 PRs delivered, 367 tests passing
  - OpenAPI + Python/TypeScript SDK auto-generation
  - OpenTelemetry distributed tracing
  - CORS / TLS / JWT secret rotation hardening
  - See [RELEASE_NOTES.md](RELEASE_NOTES.md) for the full breakdown
