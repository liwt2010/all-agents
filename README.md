# Agent System

[![CI](https://github.com/agent-system/agent-system/actions/workflows/ci.yml/badge.svg)](https://github.com/agent-system/agent-system/actions/workflows/ci.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Enterprise Multi-Agent Collaboration Platform** — an AI-powered workflow system with 9 built-in agents, plugin tools, multi-tenant isolation, and production-grade security.

```
User → CEO Agent → Product Agent → Tech Agent → Test Agent → Deploy Agent → DevOps Agent
                  ↘ Security Agent  ↘ Docs Agent    ↘ Review Agent
```

## Why Agent System?

| Instead of chatting with one AI... | You get an **AI team** |
|---|---|
| One-shot answer | Multi-step pipeline with peer review |
| Single context window | Shared memory (MultiLinkGraph) |
| Manual tool switching | Automated MCP tool discovery |
| No audit trail | Full audit logging + LLM cost tracking |

## Quick Start

```bash
# Install
pip install -e ".[api]"

# Set your API key (Anthropic, OpenAI-compatible, or local)
export ANTHROPIC_API_KEY=sk-xxx

# Run a single agent
python -m agent_system run "Write a PRD for a login feature"

# Run the full pipeline (Product → Tech → Test)
python -m agent_system pipeline "Build a todo app"

# Start the API server
uvicorn agent_system.api.server:app --port 8000
```

## Agents

| Agent | Role | Key Capabilities |
|-------|------|-----------------|
| **CEO** | Orchestrator | Task dispatch, escalation handling, pipeline management |
| **Product** | Requirements | PRD writing, feature breakdown, acceptance criteria |
| **Tech** | Implementation | Code generation, architecture design, code review |
| **Test** | Quality | Test generation, execution, coverage analysis |
| **Deploy** | Operations | Staging/prod rollout, migration execution, rollback |
| **DevOps** | Infrastructure | CI/CD, K8s, monitoring, IaC review |
| **Security** | Compliance | Secrets scanning, dependency CVE, threat modeling |
| **Docs** | Documentation | API reference, runbooks, ADRs, changelogs |
| **Review** | Peer Review | Code/design/test plan review, merge approval |

## Features

- **9 built-in agents** covering the full product lifecycle
- **Smart escalation** — SELF / PEER / HUMAN / ESCALATE (4-way decision)
- **MultiLinkGraph** — 11 node types, 23 link types, time-decayed experience memory
- **Plugin tool system** — `@register` decorator, auto-discovery, hot-reload
- **MCP protocol** — connect any MCP-compatible tool server
- **Multi-tenant** — 6-space isolation model (private → tenant public)
- **RBAC** — 6 roles, 7 permissions, permission group overrides
- **9 auto-calculated metrics** from the memory graph
- **Live progress** — WebSocket + REST progress polling with checkpoint resume
- **Security** — input sanitization, secrets detection, rate limiting, file sandbox
- **Distributed locks** — Redis-backed with in-memory fallback

## API Endpoints

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/health` | GET | No | Liveness probe |
| `/api/ready` | GET | No | Readiness probe (checks DB, LLM) |
| `/api/auth/token` | POST | No | Issue JWT (dev only) |
| `/api/agents` | GET | JWT | List available agents |
| `/api/tasks` | POST | JWT | Submit a task |
| `/api/tasks/{id}` | GET | JWT | Get task result |
| `/api/tasks` | GET | JWT | List tasks (paginated, tenant-isolated) |
| `/api/tasks/{id}/progress` | GET | JWT | Live progress |
| `/api/ws/{id}` | WS | JWT | WebSocket status stream |
| `/api/graph/stats` | GET | JWT | Graph statistics |
| `/api/metrics` | GET | JWT | Prometheus metrics |

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `ANTHROPIC_API_KEY` | — | LLM API key (required in production) |
| `AUTH_SECRET` | — | JWT signing secret (32+ chars) |
| `ENVIRONMENT` | `development` | `production` enables strict mode |
| `POSTGRES_URL` | — | Postgres connection string |
| `REDIS_URL` | — | Redis connection string |
| `RATE_LIMIT_PER_MINUTE` | 60 | Requests per IP per minute |
| `ALLOWED_FILE_ROOTS` | `data,tmp,.` | Comma-separated sandbox paths |
| `CORS_DEV_ORIGINS` | — | Additional CORS origins (dev) |
| `LLM_REQUIRE_REAL` | — | Set `1` to block mock mode in stage |

## Production Deployment

```bash
# Docker
docker-compose up --build

# Helm (K8s)
helm install agent-system ./deploy/helm \
  --set env.anthropicApiKey=$ANTHROPIC_API_KEY \
  --set env.postgresUrl=$POSTGRES_URL
```

See [docs/RUNBOOK.md](docs/RUNBOOK.md) for the operations manual.

## Screenshots

```
Dashboard:  [9 metrics cards] [Agent list] [Recent tasks]
Submit:     [Text input] [Agent select] → [Live progress bar] [Result JSON]
Tasks:      [Filter by status] [Paginated list]
Graph:      [Node/link charts] [Age distribution]
Metrics:    [Sparkline tiles] [Auto-refresh every 3s]
```

## Project Structure

```
src/agent_system/
├── agents/       # 9 built-in agents
├── api/          # FastAPI server
├── core/         # SmartAgent, LLM router, auth, RBAC, events, cache
├── memory/       # MultiLinkGraph, experience feedback
├── tools/        # Plugin tool system + MCP client
├── storage/      # Postgres + Redis backends
├── concurrency/  # Distributed lock
├── migration/    # Data migration engine
├── observability/ # Tracing + Prometheus metrics
├── config/       # ConfigManager (4-layer override)
├── auth/         # JWT + RBAC
└── onboarding/   # FTUE flow
```

## Performance

| Operation | p50 | p95 | n |
|-----------|-----|-----|---|
| Health check | 6ms | 15ms | 200 |
| Graph node lookup | 0.4μs | 0.5μs | 1000 |
| Rate limit check | 1.3μs | 2.2μs | 10000 |
| Audit append | 4μs | 8μs | 1000 |

## Roadmap

- [ ] Streaming LLM responses via WebSocket
- [ ] Custom Agent templates marketplace
- [ ] AutoGen-native peer discussion
- [ ] OpenTelemetry SDK export
- [ ] Grafana dashboard JSON
- [ ] GitHub App integration (auto PR review)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) (pending).

## License

MIT
