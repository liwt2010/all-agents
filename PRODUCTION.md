# Production Deployment Guide

> **Read time: 5 min.** Ship in 5 min with SQLite, scale to Postgres when you outgrow it.

This repo is **production-grade** — every PR is independently committable + revertable. But that doesn't mean you need a Kubernetes cluster to try it. Start with the minimal path; upgrade only when you have a concrete reason.

---

## 1. Minimal deployment — SQLite, single container, 5 minutes

The fastest path. Works on any laptop, any VM, any CI runner. No external services, no Postgres, no Redis, no Kafka.

```bash
# 1. Pull
docker pull liwt2010/all-agents:v0.1.0

# 2. Run (only required secret — generate with: python -c 'import secrets;print(secrets.token_urlsafe(48))')
docker run -d -p 8000:8000 --name agent \
  -e AUTH_SECRET="<32+ random chars>" \
  -e ENVIRONMENT=production \
  -v $PWD/data:/data \
  liwt2010/all-agents:v0.1.0

# 3. Verify
curl http://localhost:8000/api/health
# {"status":"ok","version":"0.1.0","uptime":1.23, "peer_autogen_enabled": true|false}

# 4. Submit your first task
curl -X POST http://localhost:8000/api/pipeline/run \
  -H "Authorization: Bearer <your-token>" \
  -H "Content-Type: application/json" \
  -d '{"pipeline":"code_review","input":{"code":"def add(a,b): return a+b"}}'
```

That's it. SQLite is the default backend (`AGENT_STORAGE=json` for files, `AGENT_SQLITE_PATH=data/agent.db`). One container, one volume, zero network calls.

**What works out of the box:**
- All 9 agents (Tech / Product / Test / Code / Deploy / PEER / Review / Dataview / Smart core)
- 885 tests pass locally + via CI
- Prometheus metrics at `/metrics`
- OTel tracing (CONSOLE exporter by default; set `OTEL_MODE=otlp_http` + `OTEL_EXPORTER_OTLP_ENDPOINT` for real backend)
- Backup subsystem writes to `/data/backup`
- Audit log to `/data/audit`
- Rate limiting (in-memory; swap to Redis later if you scale out)

**What is NOT included in minimal deploy** (deliberate omissions, not bugs):
- Multi-replica HA — SQLite is single-writer; use Postgres for that
- AutoGen PEER path is **optional** — if `autogen-agentchat` isn't installed, PEER falls back to lightweight DiscussionMixin. The startup banner tells you which mode you're in.

---

## 2. When to upgrade to Postgres

You only need Postgres when you hit one of these:

| Symptom | Threshold | Action |
|---|---|---|
| Single-writer contention on `agent.db` | >50 tasks/sec sustained | Move to Postgres |
| Want multi-replica deployments (>1 instance) | Any | Postgres (SQLite locks at file level) |
| Need row-level tenant isolation | Multi-tenant SaaS | Postgres + `psycopg2-binary` (already in `requirements.txt`) |
| Backup size >10GB | SQLite backup file too big to ship | Postgres `pg_dump` streams |

Postgres config (still single instance, no Kubernetes needed):

```bash
docker run -d -p 5432:5432 --name agent-pg \
  -e POSTGRES_PASSWORD=changeme \
  -e POSTGRES_DB=agent_system \
  postgres:16

docker run -d -p 8000:8000 --name agent \
  -e AUTH_SECRET="<same as before>" \
  -e AGENT_STORAGE=postgres \
  -e AGENT_POSTGRES_URL=postgresql://postgres:changeme@host.docker.internal:5432/agent_system \
  liwt2010/all-agents:v0.1.0
```

`psycopg2-binary==2.9.10` is pre-pinned in `requirements.txt` — no extra install step.

---

## 3. When to add Redis

Redis is needed for **multi-replica** deployments only. The in-memory sliding-window rate limiter works fine for single-instance.

| Setup | Need Redis? |
|---|---|
| 1 instance, <1000 RPS | No |
| 2-10 instances behind a load balancer | Yes (rate-limit + audit log fan-out) |
| 10+ instances / multi-region | Yes + Kafka/Redis Streams |

---

## 4. Production hardening checklist

Once you've decided to go beyond minimal:

- [ ] `AUTH_SECRET` is 32+ chars of random — never reuse across environments
- [ ] `ENVIRONMENT=production` set explicitly (changes CORS, HSTS, error verbosity)
- [ ] `OTEL_MODE=otlp_http` + `OTEL_EXPORTER_OTLP_ENDPOINT` pointed at a real collector (Tempo, Jaeger, Honeycomb)
- [ ] `/metrics` scraped by Prometheus (see `prometheus.yml` example below)
- [ ] `/data` mounted on a persistent volume (never an ephemeral container path)
- [ ] Backup schedule configured (cron or k8s CronJob) — see `scripts/backup.py`
- [ ] Logs shipped to a centralized store (Loki, ELK, Datadog) — `docker logs` is not a retention strategy
- [ ] Health check wired into load balancer / k8s readinessProbe (`/api/health`)
- [ ] CI green on every PR (see `DEFERRED.md` for what was fixed in v0.1.0)

Minimal `prometheus.yml` scrape:

```yaml
scrape_configs:
  - job_name: agent_system
    metrics_path: /metrics
    static_configs:
      - targets: ['agent:8000']
```

---

## 5. Environment variables reference

See `.env.example` for the full annotated list. The only **required** variable is `AUTH_SECRET`. Everything else has a sane default.

---

## 6. Smoke tests after any change

```bash
# Local
python -m pytest tests/ -q -m 'not real_llm' \
  --ignore=tests/test_pipeline_e2e_real_llm.py \
  --ignore=tests/test_resolver_peer_real_llm.py \
  --ignore=tests/test_data_provenance.py

# Real-LLM (needs API key)
ANTHROPIC_API_KEY=... python -m pytest tests/test_pipeline_e2e_real_llm.py -v

# Production-readiness gate (always run)
python -m pytest tests/test_production_readiness.py -v

# Live smoke
curl http://localhost:8000/api/health
curl http://localhost:8000/openapi.json | head
```

Expected: 861 collected (CI subset), 0 failed, 42 readiness passed, `/api/health` returns 200 with `status: ok`.

---

## 7. CI status (as of 2026-07-09)

GitHub Actions runs on every push to `main`. As of v0.1.0, the workflow is green:

- ✅ `Install dependencies` (was failing 4s-fast on PowerShell-stderr-leaked requirements.txt — fixed in commit `e90f49f`)
- ✅ `Collect (verify tests can be imported)` — fail-fast on ModuleNotFoundError
- ✅ `Run unit tests` — 861 collected, 0 failed
- ✅ `Production-readiness gate` — 42 passed

See `.github/workflows/ci.yml` for the full pipeline.

---

## 8. Where to get help

- `README.md` — project overview, three-language versions in `README.zh-CN.md` / `README.zh-TW.md`
- `RELEASE_NOTES.md` — full changelog for v0.1.0 (22 PRs)
- `STATUS.md` — what works, what doesn't, what the live numbers are
- `DEFERRED.md` — known limitations + handoff notes
- `ARCHITECTURE.md` — internal design (for contributors)
- `/docs` (Swagger UI) when the server is running