# Production Deployment Guide

**Status: PRODUCTION-READY** (verified end-to-end with real LLM API, July 2026)
**Current version: v0.6.0** (Task collaboration primitives)

This guide covers deploying and operating `all-agents` in production.
For incident response procedures, see `RUNBOOK.md`. For design
rationale, see `docs/adr/` and `ARCHITECTURE.md`. For the
implementation status of any specific feature, see `CHANGELOG.md`
or `RELEASE_NOTES.md`.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Pre-deployment Checklist](#2-pre-deployment-checklist)
3. [Environment Variables](#3-environment-variables)
4. [LLM API Key Handling](#4-llm-api-key-handling)
5. [Storage Backend Selection](#5-storage-backend-selection)
6. [Container Deployment](#6-container-deployment)
7. [Health & Readiness](#7-health--readiness)
8. [Monitoring](#8-monitoring)
9. [Backups & DR](#9-backups--dr)
10. [Performance & Scaling](#10-performance--scaling)
11. [Security](#11-security)
12. [CI / CD Gate](#12-ci--cd-gate)
13. [Incident Response](#13-incident-response)
14. [Contacts](#14-contacts)
15. [Versioning](#15-versioning)

---

## 1. System Overview

all-agents v0.6.0 is a production-grade multi-agent orchestration
platform with three transports, persistent state, observability,
and audit-grade collaboration primitives.

| Layer | Component | Notes |
|---|---|---|
| **API** | FastAPI on port 8000 | REST + WebSocket + **gRPC :50051** (v0.5.0) |
| **Auth** | RS256 JWT + JWKS endpoint (v0.2.0) | HS256 still supported for legacy |
| **Storage** | `InMemoryTaskStore` (dev) + `PostgresTaskStore` (prod) | CAS on `update_fields` (v0.6.0) |
| **Rate limit** | Sliding window — InMemory + Redis Lua backend (v0.2.0) | Pluggable; multi-replica safe with Redis |
| **Postgres RLS** | `tenant_id` GUC + `app.current_tenant` policy (v0.2.0) | Fail-closed by default |
| **Streaming** | `/api/ws/llm/stream` token + tool-call events (v0.2.0 / v0.4.0) | 15s heartbeat; cancels on disconnect |
| **gRPC** | 4 RPCs over `:50051` (v0.5.0) | `x-user-id` metadata for attribution |
| **GitHub App** | `/api/webhooks/github` HMAC + replay cache (v0.3.0) | Auto ReviewAgent on `pull_request` |
| **Custom Agents** | YAML-defined per tenant (v0.3.0) | `/api/custom-agents:upload` (admin) |
| **Collaboration** | Task `owner_id` / `assignee_id` / `version` / `visibility` (v0.6.0) | claim / handoff / events endpoints |
| **Audit** | JSONL log + `task_id` query filter (v0.2.0 / v0.6.0) | `BatchAuditLogger` + `query_from_disk` |
| **Backups** | cron + SHA-256 manifest + DR drill (v0.2.0) | See `docs/BACKUP.md` |
| **Provenance** | REAL_LLM / MOCK / LLM_FAILURE labels on every output | Badge in `metadata.data_provenance_badge` |
| **Schema validation** | 4-tier (STRICT / LENIENT / REPAIR / WARN) | Auto-repair + FailureNodeLogger |

**Test status** (latest `origin/main`, v0.6.0): **1105 passed** /
**16 skipped** (WebSocket TestClient framework bug) / **2 xfail**
(`openapi-python-client` 0.26 upstream UP007 bug) / **0 failed**.

### 1.1 Transport matrix

| Transport | URL | Auth | Use case |
|---|---|---|---|
| REST | `https://host:8000/api/*` | Bearer JWT (RS256) | Browser, curl, integration |
| WebSocket | `wss://host:8000/api/ws/llm/stream` | JWT in query | Real-time LLM tokens |
| gRPC | `host:50051` | `x-user-id` metadata (v0.6.0+) | Service-to-service, batch jobs |

---

## 2. Pre-deployment Checklist

- [ ] **LLM API key** from your provider (Anthropic / OpenAI-compatible)
- [ ] **AUTH_PRIVATE_KEY** (PEM, PKCS#8) for RS256 — generate via `python scripts/gen_rsa_keys.py --kid v1`
- [ ] **AUTH_PUBLIC_KEYS** (`kid:public_pem,...`) for token verification (include any retired keys)
- [ ] **TLS cert** for your domain (Let's Encrypt or your CA)
- [ ] **Reverse proxy** (nginx / Caddy / cloud LB) terminating TLS
- [ ] **PostgreSQL server** (>= 13) for multi-replica prod
- [ ] **Redis** (>= 6) for multi-replica rate limit / lock (optional, falls back to in-memory)
- [ ] **Backups storage** (local disk + S3 / NFS / etc.)
- [ ] **Monitoring endpoint** reachable (Prometheus scraper)
- [ ] **GITHUB_WEBHOOK_SECRET** if using GitHub App integration
- [ ] **GITHUB_BOT_USER_ID** if using GitHub App (default `github-bot`)

---

## 3. Environment Variables

All config flows through env vars (canonical list in `.env.example`).
Changes here are **breaking if missed** in deployment.

### 3.1 REQUIRED — service refuses to start without these

| Variable | Purpose | Example |
|---|---|---|
| `AUTH_PRIVATE_KEY` | RSA private key (PEM, PKCS#8) — signs new JWTs | `-----BEGIN PRIVATE KEY-----...` |
| `AUTH_PUBLIC_KEYS` | `kid:public_pem,...` for verify (RS256) | `v1:-----BEGIN PUBLIC KEY-----...` |
| `ENVIRONMENT` | `development` / `production` | `production` |
| `LLM_PROVIDER` | `anthropic` / `openai` / `mock` | `anthropic` |

**Backward compat:** if `AUTH_PRIVATE_KEY` is unset, the service
falls back to HS256 using `AUTH_SECRET` (or `AUTH_SECRETS`). HS256
remains supported but is **not recommended for multi-issuer /
multi-tenant deployments** — use RS256.

### 3.2 REQUIRED for LLM calls

| Variable | Purpose | Example |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic SDK | `sk-ant-...` |
| `OPENAI_API_KEY` | OpenAI-compatible (DeepSeek, Azure, etc.) | `sk-...` |
| `ANTHROPIC_BASE_URL` | Override Anthropic base URL (compat proxy) | `https://your-proxy.com` |
| `OPENAI_BASE_URL` | Override OpenAI base URL | `https://api.deepseek.com` |
| `LLM_MODEL` | Override default `deepseek-v4-flash` | `claude-haiku-4-5-20251001` |

**CRITICAL:** see [§4 LLM API Key Handling](#4-llm-api-key-handling)
below — keys are sensitive and never logged.

### 3.3 REQUIRED for PostgreSQL backend (production recommended)

| Variable | Purpose | Example |
|---|---|---|
| `POSTGRES_HOST` | DB host | `db.internal` |
| `POSTGRES_PORT` | DB port | `5432` |
| `POSTGRES_DB` | DB name | `all_agents` |
| `POSTGRES_USER` | DB user | `all_agents` |
| `POSTGRES_PASSWORD` | DB password | (in secret manager) |

**Postgres RLS** (v0.2.0) is applied automatically on first
connection. The migration adds a `tenant_id` column, indexes, and
RLS policies (`tenant_isolation_nodes` / `tenant_isolation_links`).
Connections without `set_tenant_id(...)` see **no rows** (fail-closed
default). Cross-tenant admin access uses a role with `BYPASSRLS`.

### 3.4 OPTIONAL — Redis (recommended for multi-replica)

| Variable | Default | Purpose |
|---|---|---|
| `REDIS_URL` | unset | `redis://host:6379/0` activates Redis rate-limit backend |
| | | Falls back to in-memory if Redis is unreachable at startup |

### 3.5 OPTIONAL — WebSocket streaming

| Variable | Default | Purpose |
|---|---|---|
| `WS_HEARTBEAT_INTERVAL` | `15.0` | Seconds between ping frames |

### 3.6 OPTIONAL — gRPC transport (v0.5.0)

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_GRPC_PORT` | `50051` | gRPC server bind port (opt-in) |

To run the gRPC listener: `pip install grpcio grpcio-tools && python -m agent_system.grpc.codegen && python -m agent_system.grpc.server`.
The `.proto` (`src/agent_system/grpc/proto/agent_system.proto`) is the
source of truth; `_pb2.py` / `_pb2_grpc.py` are generated and **gitignored**.

For client attribution (v0.6.0+), send gRPC metadata:

```python
metadata = (("x-user-id", "alice"), ("x-tenant-id", "acme"))
stub.SubmitTask(req, metadata=metadata)
```

If `x-user-id` is absent, the task's `owner_id` defaults to `"system"`
and `tenant_id` falls back to the request body. **Always send the
metadata in production** to avoid silent attribution drift.

### 3.7 OPTIONAL — GitHub App integration (v0.3.0)

| Variable | Default | Purpose |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | — | HMAC secret for `X-Hub-Signature-256` verification |
| `GITHUB_PR_COMMENT_TOKEN` | unset | Optional PAT with `repo` scope to post review back as PR comment |
| `GITHUB_BOT_USER_ID` | `github-bot` | `owner_id` for tasks created by the webhook |

### 3.8 OPTIONAL — observability

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_OTEL_ENABLED` | `false` | Enable OpenTelemetry tracing (auto-instrumented FastAPI) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4318` | OTLP/HTTP collector URL (when OTEL on) |
| `AGENT_AUDIT_RETENTION_DAYS` | `90` | Auto-purge audit files older than N days |
| `AGENT_BACKUP_CRON` | `0 2 * * *` | Daily at 02:00 UTC |
| `AGENT_BACKUP_RETENTION_DAYS` | `7` | Local backup retention |

### 3.9 OPTIONAL — rate limiting

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_RATE_LIMIT_ENABLED` | `true` | Master toggle |
| `AGENT_RATE_LIMIT_SCOPE_DEFAULT_USER` | `120` | Per-user per-minute |
| `AGENT_RATE_LIMIT_SCOPE_DEFAULT_IP` | `240` | Per-IP per-minute |
| `AGENT_RATE_LIMIT_SCOPE_EXPENSIVE_USER` | `20` | LLM-calling endpoints |
| `AGENT_RATE_LIMIT_SCOPE_AUTH_USER` | `5` | Auth endpoints (anti-brute-force) |

### 3.10 OPTIONAL — networking & misc

| Variable | Default | Purpose |
|---|---|---|
| `MAX_REQUEST_BYTES` | `1048576` | 1 MB request body cap |
| `ALLOWED_FILE_ROOTS` | `data,tmp` | File system roots agents can read |
| `CORS_ALLOWED_ORIGINS` | `http://localhost:5173,http://localhost:3000` | Comma-separated; `*` **rejected** in production |
| `TLS_REDIRECT_ENABLED` | `false` | Enable HTTP→HTTPS 301 |
| `TLS_HSTS_ENABLED` | `true` (prod) | Add HSTS header |
| `TLS_HSTS_MAX_AGE` | `31536000` | 1 year |
| `DISABLE_SECURITY_MIDDLEWARE` | unset | Set `1` ONLY for local dev |

---

## 4. LLM API Key Handling

The platform **never logs** API keys. Guards in place:

- `redact()` in `core/audit_logger.py` replaces key patterns with `***API_KEY***`
- `SENSITIVE_LOG_REDACT_PATTERNS` covers: `api[-_]?key`, `password`, `secret`, `token`, `sk-`, `ghp_`, `JWT`, `email`, `credit-card`, `SSN`
- `SecretsInRequestMiddleware` rejects inbound HTTP requests containing known secret patterns in body (GitHub/AWS/Slack/Google/JWT)

**Required operations**:

1. **Never** commit a real key to git. Use environment variables, k8s secrets, or a secret manager.
2. **Rotate keys** at least every 90 days:
   ```bash
   # Generate new key in Anthropic console
   # Update secret manager / k8s secret
   kubectl -n agent-system create secret generic agent-system-secrets \
     --from-literal=anthropic-api-key=$NEW_KEY --dry-run=client -o yaml | kubectl apply -f -
   # Restart pods to pick up new key
   kubectl -n agent-system rollout restart deploy/agent-system
   ```
3. **Audit key access**: every request is logged with a redacted key fragment in the audit log. Look up full key usage via `user_id` / `task_id` correlation.

---

## 5. Storage Backend Selection

| Use case | Backend | Config |
|---|---|---|
| Local dev / tests | `InMemoryTaskStore` (default) | no env vars needed |
| Single-node prod (low write rate) | `PostgresTaskStore` | `POSTGRES_*` env vars |
| Multi-node prod | `PostgresTaskStore` | `POSTGRES_*` + Redis for locks/rate-limit |
| DR drill | Run `python -m agent_system.core.backup.drill ...` | (see BACKUP.md) |

**Multi-node prod MUST use Postgres.** The in-memory backend is
single-process only; concurrent writes will corrupt state.

### 5.1 Postgres schema migrations

Schema migrations are **idempotent** and run automatically on
every Postgres backend connection:

```sql
-- v0.2.0 RLS migration (memory tables)
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS owner_id TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assignee_id TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'private';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_tasks_owner_id ON tasks(owner_id);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee_id ON tasks(assignee_id);
```

For greenfield deploys, these migrations create columns on a fresh
table. For upgrades from pre-v0.6.0, they **add** the columns with
safe defaults so existing rows continue to load.

Cross-tenant admin via `BYPASSRLS`:

```sql
ALTER ROLE all_agents_admin BYPASSRLS;
```

### 5.2 Task CAS (v0.6.0)

`TaskStore.update_fields(id, expected_version, **fields)` is the
atomic boundary. The Postgres impl uses:

```sql
UPDATE tasks SET ..., version = version + 1, updated_at = now()
WHERE id = :id AND version = :ev RETURNING *
```

`rowcount == 0` means CAS conflict — caller gets `VersionConflict`
with the current record. `POST /api/tasks/{id}/claim` and
`/handoff` surface this as HTTP 409 with the current state in the
detail body, so clients can refresh and retry without a separate GET.

---

## 6. Container Deployment

### 6.1 Docker Compose (single-host, dev or small prod)

```yaml
# docker-compose.yml (already in repo root)
services:
  api:
    build: .
    env_file: .env
    ports:
      - "8000:8000"   # REST + WebSocket
      - "50051:50051" # gRPC (v0.5.0, optional)
    volumes:
      - /var/lib/agent-system/data:/app/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"]
      interval: 30s
      timeout: 10s
      retries: 3
```

```bash
docker compose up -d
docker compose logs -f api
curl http://localhost:8000/api/health
curl http://localhost:8000/api/ready
```

### 6.2 Kubernetes (production multi-node)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: agent-system
  namespace: agent-system
spec:
  replicas: 3
  selector:
    matchLabels:
      app: agent-system
  template:
    metadata:
      labels:
        app: agent-system
    spec:
      containers:
      - name: api
        image: your-registry/agent-system:v0.6.0
        ports:
        - containerPort: 8000   # REST + WebSocket
        - containerPort: 50051  # gRPC (v0.5.0)
        envFrom:
        - secretRef:
            name: agent-system-secrets
            # contents: ANTHROPIC_API_KEY, AUTH_PRIVATE_KEY, AUTH_PUBLIC_KEYS, ...
        env:
        - name: ENVIRONMENT
          value: "production"
        - name: LLM_PROVIDER
          value: "anthropic"
        - name: POSTGRES_HOST
          value: postgres
        - name: POSTGRES_PORT
          value: "5432"
        - name: POSTGRES_DB
          value: all_agents
        - name: REDIS_URL
          value: redis://redis:6379/0
        - name: CORS_ALLOWED_ORIGINS
          value: "https://app.example.com,https://admin.example.com"
        - name: AGENT_GRPC_PORT
          value: "50051"
        livenessProbe:
          httpGet:
            path: /api/health
            port: 8000
          initialDelaySeconds: 30
          periodSeconds: 30
        readinessProbe:
          httpGet:
            path: /api/ready
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 10
        resources:
          requests:
            cpu: 500m
            memory: 1Gi
          limits:
            cpu: 2
            memory: 4Gi
        volumeMounts:
        - name: data
          mountPath: /app/data
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: agent-system-data
---
apiVersion: v1
kind: Service
metadata:
  name: agent-system
  namespace: agent-system
spec:
  selector:
    app: agent-system
  ports:
  - name: http
    port: 80
    targetPort: 8000
  - name: grpc
    port: 50051
    targetPort: 50051
```

### 6.3 Pre-deploy verification

```bash
# Tag exists in your registry
docker pull your-registry/agent-system:v0.6.0

# Schema migration is idempotent
python -c "
from agent_system.storage.task_store import PostgresTaskStore
store = PostgresTaskStore('postgresql://...')
print('connected, migrations applied')
"

# Generate / rotate RSA keys if first deploy
python scripts/gen_rsa_keys.py --kid v1
# This writes AUTH_PRIVATE_KEY and AUTH_PUBLIC_KEYS to stdout.
# Pipe to k8s secret / .env (NOT to git).
```

---

## 7. Health & Readiness

| Endpoint | Purpose | Returns 200 when |
|---|---|---|
| `GET /api/health` | **Liveness** (am I alive?) | Process is running |
| `GET /api/ready` | **Readiness** (can I serve traffic?) | Storage + LLM configured (production requires real key) |

Both unauthenticated. K8s `livenessProbe` should use `/api/health`,
`readinessProbe` should use `/api/ready`. Pod restarts on liveness
failure; removed from LB on readiness failure.

---

## 8. Monitoring

### 8.1 Prometheus scrape config

```yaml
scrape_configs:
  - job_name: 'agent-system'
    metrics_path: '/metrics'
    static_configs:
      - targets: ['agent-system:8000']
    scrape_interval: 30s
```

### 8.2 Key metrics

| Metric | Type | Purpose |
|---|---|---|
| `agent_http_requests_total{method,path,status}` | counter | Request rate by status |
| `agent_http_request_duration_seconds{method,path}` | histogram | Latency p50/p95/p99 |
| `agent_tasks_total{agent_type,status}` | counter | Task success/failure rate |
| `agent_task_duration_seconds{agent_type}` | histogram | Task latency |
| `agent_llm_requests_total{model,provider,status}` | counter | LLM call rate |
| `agent_llm_tokens_total{model,type}` | counter | Token consumption (cost tracking) |
| `agent_llm_request_duration_seconds{model}` | histogram | LLM call latency |
| `agent_storage_ops_total{backend,op,result}` | counter | DB op rate |
| `agent_active_tasks` | gauge | In-flight tasks |

### 8.3 Audit log query

```bash
# Auth required: bearer token
curl -H "Authorization: Bearer $TOKEN" \
  "https://agent.example.com/api/audit/query?user_id=alice&action=task.claimed&start_date=2026-07-01&end_date=2026-07-09&limit=100"
```

Available filters: `user_id`, `action`, `outcome`, `start_date`,
`end_date`, `request_id`, **`task_id`** (v0.6.0 — also matches
legacy `resource_type='task' + resource_id=task_id` entries).

### 8.4 Log aggregation

By default logs go to stdout. Pipe to your aggregator (Loki /
ELK / Datadog). Sensitive fields are auto-redacted by `redact()`.

---

## 9. Backups & DR

### 9.1 Schedule

Default: daily 02:00 UTC. Configure via `AGENT_BACKUP_CRON`.

### 9.2 Manual backup

```bash
docker exec agent-system-api python -c "
from agent_system.core.backup import create_backup
from agent_system.core.backup.scheduler import load_backup_config_from_env
config = load_backup_config_from_env()
m = create_backup(config, storage_backend='postgres')
print(m.backup_id, m.size_bytes)
"
```

### 9.3 Restore

```bash
python -m agent_system.core.backup.restore \
  --from /var/backups/agent-system/backup-2026-07-09-020000.tar.gz \
  --target-dir /tmp/restore-test \
  --verify
# If verification passes, copy to actual data dir (manual).
```

### 9.4 DR drill

```bash
python -m agent_system.core.backup.drill \
  --from /var/backups/agent-system/backup-2026-07-09-020000.tar.gz \
  --target-dir /tmp/drill \
  --verify-queries
# Reports PASS/FAIL with detailed steps.
```

---

## 10. Performance & Scaling

### 10.1 Production targets

| Metric | Target | Notes |
|---|---|---|
| REST p50 latency | < 100ms | In-memory tasks; with CAS a small extra overhead |
| REST p95 latency | < 500ms | `list_tasks` filters in memory (v0.6.0 — future SQL pushdown) |
| WebSocket first-token | < 200ms | Mock; real LLM depends on provider |
| gRPC SubmitTask | < 50ms | Without LLM call |
| Storage read p95 | < 50ms | Postgres single-row lookup |
| Storage write p95 | < 100ms | Postgres single-row CAS UPDATE |

### 10.2 Horizontal scaling

The API layer is **stateless**: sessions in JWT (RS256), not in-memory.
Scale by:
- Adding Kubernetes replicas (3 → N)
- Putting a load balancer in front (nginx-ingress / cloud LB)
- Using Postgres backend (in-memory is single-process)
- Using Redis for rate-limit (multi-replica-safe sliding window)

Per-request state is just `RequestIDMiddleware` contextvar +
`TaskContext`. No shared mutable state.

### 10.3 Vertical scaling

- **Memory**: 2 GB minimum, 4 GB recommended (LLM responses held in memory)
- **CPU**: 2 cores minimum (LLM JSON parsing is CPU-bound)
- **Disk**: 10 GB minimum (audit logs grow ~100 MB/day at default sampling; rotate via `AGENT_AUDIT_RETENTION_DAYS`)

### 10.4 Known scaling limits (v0.6.0)

- `list_tasks` post-filters in memory. SQL pushdown is post-v0.6.0.
  For tenants with > 100k tasks this becomes the bottleneck.
- Single-process execution. Tasks run in `asyncio.create_task`
  inside the request handler. No distributed worker pool.
  Throughput is bounded by the per-replica event loop.
- gRPC `ListTasks` emits one `ListTasksResponse` per row. For large
  result sets, batch into pages.

---

## 11. Security

### 11.1 Authentication — RS256 JWT (v0.2.0)

`AuthService` auto-detects algorithm from env:

- `AUTH_PRIVATE_KEY` set → **RS256** (asymmetric; recommended for
  multi-issuer / multi-tenant / external verifiers)
- Otherwise → HS256 (legacy; single-key; backward compat)

**Public key distribution via `GET /api/auth/jwks`** (RFC 7517).
External services fetch once, cache, and verify tokens locally
without contacting the auth server. `scripts/gen_rsa_keys.py`
generates 2048 / 3072 / 4096-bit RSA keypairs with optional
`--env-file` mode.

Example keys flow:

```bash
# Generate keypair
python scripts/gen_rsa_keys.py --kid v1 --bits 3072

# Verify a token externally (no need to hit the auth server)
python -c "
import jwt
pub = open('public.pem').read()
token = 'eyJhbGc...'
print(jwt.decode(token, pub, algorithms=['RS256'], audience='agent-system'))
"
```

### 11.2 Authorization — RBAC + AccessControl (v0.2.0 / v0.6.0)

- **RBAC**: 6 roles × 7 permissions. Defined in `core/auth/`.
- **AccessControl** (6-space model from `ARCHITECTURE.md` §11):
  `private` / `perm_group` / `group` / `project` / `external` /
  `tenant_public`. Tenant isolation is hard (cross-tenant = 404).
  Wired into task routes as of v0.6.0 (`_to_user_ctx` /
  `_record_to_resource` / `_ensure_can_read`).

### 11.3 Task collaboration (v0.6.0)

| Action | Who can do it | CAS |
|---|---|---|
| `POST /api/tasks/{id}/claim` | PRIVATE: owner only. Other visibilities: any reader. | optional `expected_version` |
| `POST /api/tasks/{id}/handoff` | Owner / current assignee / platform_admin | `expected_version` required for safe concurrency |
| `GET /api/tasks/{id}/events` | Same as task read ACL | n/a |

Concurrent updates that mismatch `expected_version` return HTTP 409
with the **current record in the detail**, so the client can refresh
and retry without a separate GET round-trip.

`owner_id` is **immutable** (whitelist enforced in `TaskStore.update_fields`).
This guarantees audit integrity — you cannot reassign who created
a task, only who currently owns it via `assignee_id`.

### 11.4 Middleware chain

- `RequestIDMiddleware` — `X-Request-ID` propagation
- `SecurityHeadersMiddleware` — CSP, HSTS, X-Frame-Options
- `SlidingWindowRateLimitMiddleware` — IP + per-user + per-scope
- `RequestSizeLimitMiddleware` — 1 MB default cap
- `SecretsInRequestMiddleware` — rejects known secret patterns
- `InputSanitizer` — prompt injection detection (TrustLevel-aware)
- `CORS` — production rejects wildcard, enforces https://
- `HSTSHeaderMiddleware` — `Strict-Transport-Security` (on in prod)
- `HTTPSRedirectMiddleware` — HTTP→HTTPS 301 (off by default; enable when not behind TLS-terminating LB)
- `SecureCookieChecker` — enforces `Secure` flag on cookies

### 11.5 TLS / HTTPS

Two strategies supported:

**A. Behind a TLS-terminating LB** (cloud LB, nginx-ingress, CloudFront)
- Do **NOT** set `TLS_REDIRECT_ENABLED=true` (the LB does the redirect).
- Set `TLS_HSTS_ENABLED=true` so the app adds the HSTS header itself.
- Configure your LB to set `X-Forwarded-Proto: https` so the app sees the request as HTTPS.

**B. Direct TLS** (the app is the TLS endpoint)
- Set `TLS_REDIRECT_ENABLED=true` to enable 301 redirects from HTTP→HTTPS.
- Use a TLS terminator in front (Caddy / nginx / Traefik) that sets `X-Forwarded-Proto: https`.
- App enforces HSTS via `TLS_HSTS_ENABLED=true`.

Example ingress-nginx:

```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/force-ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-set-headers: |
      X-Forwarded-Proto: https
spec:
  tls:
  - hosts: [api.example.com]
    secretName: api-tls
  rules:
  - host: api.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: agent-system
            port: { number: 8000 }
```

### 11.6 JWT secret rotation

For RS256, the rotation model is **add a new key**:

1. Generate a new keypair:
   ```bash
   python scripts/gen_rsa_keys.py --kid v2 --bits 3072
   ```
2. Update `AUTH_PUBLIC_KEYS` to include the new key (verify list):
   ```
   AUTH_PUBLIC_KEYS=v2:-----BEGIN PUBLIC KEY-----\n...,v1:-----BEGIN PUBLIC KEY-----\n...
   ```
3. Update `AUTH_PRIVATE_KEY` to the new private key (sign list).
4. Roll the deployment. The app now signs new tokens with v2 but
   still verifies v1 tokens (graceful rollover).
5. Wait at least 1 token TTL (default 3600s = 1h) before removing v1.
6. Update `AUTH_PUBLIC_KEYS` to remove v1.

For legacy HS256 (`AUTH_SECRETS="kid:secret,..."`), the
**first entry is the signing key**; later entries are verify-only.
Rotation = prepend the new key.

### 11.7 gRPC security (v0.5.0 / v0.6.0)

The gRPC transport currently uses **plaintext** by default
(`add_insecure_port`). Production deployments should:

1. Run gRPC behind the same TLS-terminating reverse proxy as REST
   (h2c → h2 upgrade), OR generate TLS certs and use `add_secure_port`.
2. **Always send `x-user-id` / `x-tenant-id` metadata** to attribute
   the call. Without metadata, owner_id defaults to `"system"` —
   this is intentional for trusted internal services but **unsafe
   for any multi-tenant deployment**.

A `ServerInterceptor` for auth enforcement is post-v0.6.0 (tracked).

### 11.8 Recommended additional hardening

1. **TLS**: terminate at reverse proxy. Never expose port 8000 directly.
2. **Network policy**: PostgreSQL on a private network, not internet-routable.
3. **Secrets manager**: use AWS Secrets Manager / HashiCorp Vault / k8s secrets, not env files.
4. **Image scanning**: scan Dockerfile images with Trivy / Snyk before deploy.
5. **SBOM**: maintain a Software Bill of Materials for compliance.
6. **GitHub App**: keep `GITHUB_WEBHOOK_SECRET` rotated; restrict `GITHUB_PR_COMMENT_TOKEN` to `repo` scope only.

### 11.9 Compliance notes

- All sensitive fields auto-redacted in logs (`redact()`).
- Audit log retains 90 days default (configurable via `AGENT_AUDIT_RETENTION_DAYS`).
- Per-tenant isolation via `tenant_id` + Postgres RLS fail-closed.
- RBAC: 6 roles, 7 permissions (see `core/auth/`).
- CORS denies wildcard in production.
- TLS enforced via HSTS + (optional) HTTPS redirect.
- JWT secret rotation with no-downtime (RS256 preferred).
- Task `owner_id` immutable; `version` field for CAS.

---

## 12. CI / CD Gate

Before merge to `main`, the following **must pass** (enforced by
`.github/workflows/ci.yml`):

1. **Unit tests**: `pytest tests/ -q --ignore=tests/test_*real_llm.py` — 1105+ tests
2. **Production-readiness check**: `pytest tests/test_production_readiness.py -v` — verifies all production artifacts exist (OpenAPI, README, env example, Dockerfile, etc.)
3. **Lint**: ruff check on `src/`
4. **Schema migration compatibility**: Postgres `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` is idempotent; runs on every connection

**Real-LLM smoke** runs weekly (manual dispatch):

```bash
ANTHROPIC_API_KEY=sk-ant-... pytest tests/test_*real_llm.py -v
```

PRs without these green lights are blocked from merge. The
`test_production_readiness.py` test is the production-readiness gate.

---

## 13. Incident Response

Full procedures in `RUNBOOK.md`. Tier summary:

| Tier | Response time | Examples |
|---|---|---|
| 1 | 5 min | LLM rate-limited, DB pool exhausted, single agent stuck |
| 2 | 15 min | Auth service unreachable, audit pipeline blocked, MCP down |
| 3 | 30 min | Whole system down, data corruption, secret leaked |
| 4 | (CTO + CEO) | K8s cluster down, cross-region failure, major security incident |

For v0.6.0 specifically:

- **`VersionConflict` storm**: if many clients are claiming the same task simultaneously and getting 409, check the audit log for `task.claimed` rate; possible runaway client.
- **`Visibility=private` blocking access**: client reports 403 on a task they previously saw. Verify their `tenant_id` matches and they have the right `perm_group_ids` / `shared_with` membership.
- **gRPC `owner_id="system"`**: indicates clients aren't sending `x-user-id` metadata. Enable TLS, audit who calls without metadata.

---

## 14. Contacts

- **Platform on-call**: pager (your company)
- **Security**: security@yourcompany.com
- **Compliance**: compliance@yourcompany.com
- **LLM vendor**: Anthropic support (https://support.anthropic.com)

---

## 15. Versioning

This document applies to all-agents **v0.6.0 and later**. v0.6.0
ships:

- RS256 JWT + JWKS endpoint
- Redis rate-limit backend
- PostgreSQL row-level security
- WebSocket LLM streaming (text + tool-call events)
- GitHub App integration
- Custom Agent marketplace
- Native gRPC transport
- Task collaboration primitives (CAS, claim, handoff, events)

For changes, edit this file and update the "Status" header at the
top. Older versions:

| Version | Notable |
|---|---|
| v0.5.0 | Native gRPC transport |
| v0.4.0 | Streaming tool-call events |
| v0.3.0 | Custom Agent marketplace + GitHub App |
| v0.2.0 | RS256 JWT, Redis rate-limit, Postgres RLS, WebSocket streaming |
| v0.1.x | Initial release |

See `CHANGELOG.md` for the per-PR record.