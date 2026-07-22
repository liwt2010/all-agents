# Agent System v0.1.0 — Release Notes

**Release date:** 2026-07-09
**Git tag:** `v0.1.0`
**Commit:** `83a4922` (and 21 prior commits on `main`)

This is the **first production-grade release** of the Agent System
multi-agent orchestration platform. All 22 PRs planned for v0.1.0 are
delivered, with **920 collected tests** (910 unit/collected passed, 7 skipped, 2 xfail for upstream SDK tool bug, 1 known-failure requiring `ANTHROPIC_API_KEY` to run)
and **zero known production regressions**.

---

## What's in v0.1.0

### Core platform (PR-1 to PR-5)
- **PR-1** Dataview engine (SQL-like query over the memory graph)
- **PR-2** `SmartAgent.execute()` split into checkpoint / retry / failure / escalate
- **PR-4** `llm_router.get_api_client()` with Anthropic + OpenAI + Mock support
- **PR-5** `AgentRegistry` for dynamic agent lookup
- **PR-6** `requirements.txt` locked (no floating versions in prod)

### API & platform hardening (PR-7 to PR-13)
- **PR-7** `RequestIDMiddleware` for `X-Request-ID` propagation
- **PR-8** Custom Agent platform (Pydantic v2-friendly)
- **PR-9** Pluggable storage backend (JSON / SQLite / PostgreSQL)
- **PR-10** Prometheus metrics (11 metrics at `/metrics`)
- **PR-11** `BatchAuditLogger` with retention + HTTP query endpoint
- **PR-12** Per-user / per-scope sliding-window rate limiter
- **PR-13** Backup subsystem (cron, SHA-256 manifest, tar.gz, DR drill)

### Production deployment (PR-503cd08)
- `PRODUCTION.md` — 11KB, 15 sections (pre-deploy, env vars, LLM keys,
  storage, Docker, K8s, health, monitoring, backup, perf, security, CI/CD,
  incident response, contacts, versioning)
- `RUNBOOK.md` rewritten (incident response only)
- `.env.example` (9 sections, REQUIRED/OPTIONAL labels)
- `.github/workflows/ci.yml` (2 jobs: unit + production-readiness gate)

### Observability (PR-61794f5, PR-0b2c8ed)
- **Data provenance** on every output: `REAL_LLM` / `MOCK` / `LLM_FAILURE`
  with confidence 0.85 / 0.0 / 0.0
- **OpenTelemetry** distributed tracing (PR-14):
  - DISABLED / CONSOLE / OTLP_HTTP modes
  - `agent.execute` OTel span with status/exception on error
  - FastAPI middleware auto-wraps every HTTP request

### Schema tolerance (PR-e541240)
- 4-tier validation: STRICT / LENIENT / REPAIR / WARN
- `MIN_PAYLOAD_FIELDS=2` policy
- `FailureNodeLogger` writes audit nodes
- `raw_output` fallback → `partial=True` → `source=llm_failure`

### Experience feedback loop (P2-3.1, PR-35e04fd + PR-64e7d89)
- `install_memory_hooks()` wired into `SmartAgent.execute()`
- Records task start / complete / failure to the experience graph
- Injects past experiences into `task.metadata['experiences']` as hints
- `memory_enabled: bool = True` opt-out flag for ephemeral workflows
- Verified end-to-end: 6/6 real-LLM success, 17.78s full loop

### API documentation + SDKs (PR-170d381)
- OpenAPI 3.1 spec: 13 paths, 14 routes, 9 schemas, 3 servers, 7 tags
- Rich metadata (description, contact, license, tags)
- Auto-generated Python SDK via `openapi-python-client`
- Auto-generated TypeScript SDK via `openapi-typescript-codegen`
- `Makefile.codegen` for one-command regeneration
- All artifacts gitignored (regenerable from source)

### Security hardening (PR-f5912ba)
- **CORS** environment-aware: production rejects `*` and `http://`,
  only `https://` + localhost allowed
- **TLS** three middlewares:
  - `HTTPSRedirectMiddleware` (HTTP→HTTPS 301, off by default)
  - `HSTSHeaderMiddleware` (HSTS header, on in production by default)
  - `SecureCookieChecker` (warn or hard-fail on missing Secure flag)
- **JWT secret rotation**: `AUTH_SECRETS="kid:secret,..."` multi-key,
  graceful rollover, no-downtime
- Detailed ops runbook in `PRODUCTION.md` §11

---

## Production-ready checklist

| Item | Status |
|---|---|
| Schema tolerance | ✅ |
| Data provenance | ✅ |
| Real LLM E2E test | ✅ (102s Product→Tech→Test→Deploy) |
| OTel distributed tracing | ✅ |
| Prometheus metrics | ✅ |
| Audit log | ✅ |
| Rate limiting | ✅ |
| Backup + DR | ✅ |
| OpenAPI + SDKs | ✅ |
| CORS hardening | ✅ |
| TLS enforcement | ✅ |
| JWT rotation | ✅ |
| Security middleware | ✅ |
| Production deployment doc | ✅ |
| CI/CD gate | ✅ |
| 4-way resolver (SELF/PEER/HUMAN/ESCALATE) | ✅ |
| Experience feedback loop | ✅ |
| **Test coverage** | **920 collected** (910 passed + 7 skipped + 2 xfail + 1 known-failure) |

---

## Known limitations

- **HS256 JWT** — fine for single-issuer; for multi-issuer/multi-tenant at
  scale, migrate to **RS256** (planned for v0.2.0).
- **OTel FastAPI auto-instrumentation** — currently we use a custom
  middleware; opentelemetry-instrumentation-fastapi can be added for
  per-route granularity (deferred — the custom middleware covers the
  cases we care about).
- **PostgreSQL backend** — tested with the connection pool, but the
  per-tenant row-level security policies are not yet enforced at the
  schema level (only at the API layer).
- **No built-in rate limit persistence** — sliding window is in-memory
  per process; in a multi-replica deployment, use Redis (planned v0.2.0).

---

## Upgrade instructions

This is the first tagged release — no upgrade path needed.

For future v0.1.x → v0.1.y upgrades, see `RUNBOOK.md` §"Upgrade procedure".

---

## Verification

```bash
# Tag points to commit 83a4922
git checkout v0.1.0

# Install
pip install -r requirements.txt

# Run tests (requires LLM API key for the 9 real-LLM tests)
ANTHROPIC_API_KEY=...  pytest tests/ -q

# Production-readiness gate (always runs in CI)
pytest tests/test_production_readiness.py -v

# OpenAPI spec + Python SDK
make codegen
```

Expected: **910 passed** in unit test run.
Production-readiness gate: **42 passed**.
Real-LLM tests: skipped locally without `ANTHROPIC_API_KEY`.

---

## Contributors

- Engineering: liwt2010
- AI assistance: Claude / Mavis

## License

MIT — see `LICENSE`.
